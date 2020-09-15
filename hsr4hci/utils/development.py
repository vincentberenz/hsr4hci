"""
Experimental utility functions that are still under heavy development,
and for which it is not fully clear whether or not they will actually
be useful in the end.
"""

# -----------------------------------------------------------------------------
# IMPORTS
# -----------------------------------------------------------------------------

from cmath import polar
from typing import Callable, Dict, List, Optional, Tuple, Union

from sklearn.linear_model import LinearRegression
from sklearn.preprocessing import StandardScaler
from tqdm.auto import tqdm

import numpy as np

from hsr4hci.utils.fitting import moffat_2d, fit_2d_function
from hsr4hci.utils.general import crop_center, rotate_position
from hsr4hci.utils.masking import get_positions_from_mask
from hsr4hci.utils.splitting import TrainTestSplitter


# -----------------------------------------------------------------------------
# FUNCTION DEFINITIONS
# -----------------------------------------------------------------------------

def smooth(array: np.ndarray, window_size: int = 10) -> np.ndarray:
    """
    Smooth by rolling average: Convolve the given `array` with a
    rectangle function of the given `window_size`

    Args:
        array: A 1D numpy array.
        window_size: A positive integer, specifying the width of the
            rectangle with which `array` is convolved.

    Returns:
        A smoothed version of `array`.
    """

    return np.convolve(array, np.ones(window_size) / window_size, mode='same')


def get_effective_pixel_width(
    position: Tuple[int, int], center: Tuple[float, float],
) -> float:
    """
    Compute the "effective" width of a pixel, that is, the the length of
    the path of a planet that crosses the center of the given pixel.

    This value will be between 1 and sqrt(2), depending on the position
    of the pixel (namely, it is a function of the polar angle).

    Args:
        position: A tuple (x, y) specifying the position of a pixel.
        center: A tuple (c_x, c_y) specifying the frame center.

    Returns:
        A value from [0, sqrt(2)] that is the "effective" pixel width.
    """

    # Get polar angle and make sure it is in [0, pi / 2], so that we do not
    # have to distinguish between different quadrants
    _, phi = polar(complex(position[1] - center[1], position[0] - center[0]))
    _, phi = divmod(phi, np.pi / 2)

    # Compute the effective pixel width, which is effectively either the
    # secans (= 1/cos) or cosecans (= 1/sin) of the polar angle
    effective_pixel_width = min(float(1 / np.cos(phi)), float(1 / np.sin(phi)))

    return effective_pixel_width


def get_signal_length(
    position: Tuple[int, int],
    signal_time: int,
    center: Tuple[float, float],
    parang: np.ndarray,
    psf_diameter: float,
) -> Tuple[int, int]:
    """
    Get a simple analytical estimate of the length (in units of frames)
    that a planet (with a PSF of the given `psf_diameter`) passing
    through a pixel at the given `position` and time `frame_idx` will
    produce, that is, the number of (consecutive) frames that will
    contain planet signal.

    Taking the `signal_time` into account is necessary because the
    temporal derivative of the parallactic angle is non-constant over
    the course of an observation, but can change up to around 50% for
    some data sets.

    In theory, a more exact estimate for this number can be achieved
    using proper forward modeling, but that takes about O(10^4) times
    longer, and still involves some arbitrary choices, meaning that
    there is no real *true* value anyway.

    Args:
        position:
        signal_time:
        center:
        parang:
        psf_diameter:

    Returns:

    """

    # Check if the parallactic angles are sorted in ascending order
    if np.allclose(parang, sorted(parang)):
        ascending = True
    elif np.allclose(parang, sorted(parang, reverse=True)):
        ascending = False
    else:
        raise ValueError('parang is not sorted!')

    # Compute radius of position
    radius = np.sqrt(
        (position[0] - center[0]) ** 2 + (position[1] - center[1]) ** 2
    )

    # Compute the effective pixel width of the position
    effective_pixel_width = get_effective_pixel_width(
        position=position, center=center
    )

    # Convert "effective pixel width + PSF diameter" to an angle at this radius
    # using the cosine theorem. First, compute the length of the side that we
    # want to convert into an angle:
    side_length = effective_pixel_width + psf_diameter

    # Degenerate case: for too small separations, if the center is ever on the
    # pixel, the pixel will *always* contain planet signal.
    if side_length > 2 * radius:
        return 0, len(parang)

    # Otherwise, we can convert the side length into an angle
    gamma = np.arccos(1 - side_length ** 2 / (2 * radius ** 2))

    # Find positions
    value_1 = parang[signal_time] - np.rad2deg(gamma) / 2
    value_2 = parang[signal_time] + np.rad2deg(gamma) / 2
    if ascending:
        position_1 = np.searchsorted(parang, value_1, side='left')
        position_2 = np.searchsorted(parang, value_2, side='right')
    else:
        position_2 = np.searchsorted(-parang, -value_1, side='left')
        position_1 = np.searchsorted(-parang, -value_2, side='right')

    # Compute the length before and after the peak (because the signal will, in
    # general, not be symmetric around the peak)
    length_1 = int(1.2 * (signal_time - position_1))
    length_2 = int(1.2 * (position_2 - signal_time))

    return length_1, length_2


def get_noise_signal_masks(
    position: Tuple[int, int],
    parang: np.ndarray,
    n_signal_times: int,
    frame_size: Tuple[int, int],
    psf_diameter: float,
    max_signal_length: float = 0.7,
) -> List[Tuple[np.ndarray, np.ndarray, int]]:
    """
    Generate the masks for training a series of models where different
    possible planet positions are masked out during training.

    This function places `n_signal_times` points in time uniformly over
    the course of the whole observation. For each such time point, we
    then assume that the planet signal is present at this point in time,
    and generate a binary mask that indicates all points in time that,
    under this hypothesis, would also contain planet signal.

    Args:
        position: An integer tuple `(x, y)` specifying the spatial
            position of the pixel for which we are computing the masks.
        parang: A numpy array of shape `(n_frames, )` containing the
            parallactic angles.
        n_signal_times: The number of different possible temporal
            positions of the planet signal for which to return a mask.
        frame_size: A tuple `(width, height)` specifying the spatial
            size of the stack.
        psf_diameter: The diameter of the PSF template (in pixels).
        max_signal_length: A value in [0.0, 1.0] which describes the
            maximum value of `expected_signal_length / n_frames`, which
            will determine for which pixels we do not want to use the
            "mask out a potential signal region"-approach, because the
            potential signal region is too large to leave us with a
            reasonable amount of training data.

    Returns:
        This function returns a list of up to `n_position` 3-tuples
        of the following form:
            `(noise_mask, signal_mask, signal_time)`.
        The first two elements are 1D binary numpy arrays of length
        `n_frames`, whereas the last element is an integer giving the
        position of the peak of the planet signal.
    """

    # Define shortcuts
    n_frames = len(parang)
    center = (frame_size[0] / 2, frame_size[1] / 2)

    # Initialize lists in which we store the results
    results = list()

    # Generate `n_signal_times` different possible points in time (distributed
    # uniformly over the observation) at which we planet signal could be
    signal_times = np.linspace(0, n_frames - 1, n_signal_times)

    # Loop over all these time points to generate the corresponding indices
    for signal_time in signal_times:

        # Make sure the signal time is an integer (we use it as an index)
        signal_time = int(signal_time)

        # Compute the expected signal length at this position and time
        length_1, length_2 = get_signal_length(
            position=position,
            signal_time=signal_time,
            center=center,
            parang=parang,
            psf_diameter=psf_diameter,
        )

        # Check if the expected signal length is larger than the threshold.
        # In this case, we do not compute the noise and signal masks, but
        # skip this signal time.
        if (length_1 + length_2) / n_frames > max_signal_length:
            continue

        # Construct the signal mask
        signal_mask = np.full(n_frames, False)
        position_1 = max(0, signal_time - length_1)
        position_2 = min(n_frames, signal_time + length_2)
        signal_mask[position_1:signal_time] = True
        signal_mask[signal_time:position_2] = True

        # Compute noise_mask as the complement of the signal_mask
        noise_mask = np.logical_not(signal_mask)

        # Store the current (noise_mask, signal_mask, signal_time) tuple
        results.append((noise_mask, signal_mask, signal_time))

    return results


def get_psf_diameter(
    psf_template: np.ndarray,
    pixscale: Optional[float] = None,
    lambda_over_d: Optional[float] = None,
) -> float:
    """
    Fit a 2D Moffat function to the given PSF template to estimate
    the diameter of the central "blob" in pixels.

    The diameter is computed at the arithmetic mean of the FWHM in
    x and y direction, as returned by the fit.

    Args:
        psf_template: A 2D numpy array containing the raw, unsaturated
            PSF template.
        pixscale:
        lambda_over_d:

    Returns:
        The diameter of the PSF template in pixels.
    """

    # Case 1: We have been provided a suitable PSF template and can determine
    # the size by fitting the PSF with a Moffat function
    if psf_template.shape[0] >= 33 and psf_template.shape[1] >= 33:

        # Crop PSF template: too large templates (which are mostly zeros) can
        # cause problems when fitting them with a 2D Moffat function
        psf_template = crop_center(psf_template, (33, 33))

        # Define shortcuts
        psf_center_x = float(psf_template.shape[0] / 2)
        psf_center_y = float(psf_template.shape[1] / 2)

        # Define initial guess for parameters
        p0 = (psf_center_x, psf_center_y, 1, 1, 1, 0, 0, 1)

        # Fit the PSF template with a 2D Moffat function to get the FWHMs
        params = fit_2d_function(frame=psf_template, function=moffat_2d, p0=p0)

        # Compute the PSF diameter as the mean of the two FWHM values
        fwhm_x, fwhm_y = params[2:4]
        psf_diameter = float(0.5 * (fwhm_x + fwhm_y))

    # Case 2: We do not have PSF template, but the PIXSCALE and LAMBDA_OVER_D
    elif (pixscale is not None) and (lambda_over_d is not None):

        # In this case, we can approximately compute the expected PSF size.
        # The 1.144 is a magic number to get closer to the empirical estimate
        # from data sets where a PSF template is available.
        psf_diameter = lambda_over_d / pixscale * 1.144

    # Case 3: In all other scenarios, we raise an error
    else:
        raise RuntimeError('Could not determine PSF diameter')

    return psf_diameter


def has_bump(
    array: np.ndarray, signal_idx: np.ndarray, signal_time: int,
) -> bool:
    """
    Check if a given `array` (typically residuals) has a positive bump
    in the region that is indicated by the given `idx`.

    Currently, the used heuristic is extremely simple:
    We split the search region into two parts, based on the given
    `signal_time`, and fit both parts with a linear model.
    If the first regression returns a positive slope, and the second
    regression returns a negative slope, the function returns True.

    Args:
        array: A 1D numpy array in which we search for a bump.
        signal_idx: A 1D numpy array indicating the search region.
        signal_time: The index specifying the "exact" location
            where the peak of the bump should be located.

    Returns:
        Whether or not the given `array` contains a positive bump in
        the given search region.
    """

    # Get the start and end position of the signal_idx
    all_idx = np.arange(len(array))
    signal_start, signal_end = all_idx[signal_idx][np.array([0, -1])]

    # Prepare predictors and targets for the two linear fits
    predictors_1 = np.arange(signal_start, signal_time).reshape(-1, 1)
    predictors_2 = np.arange(signal_time, signal_end).reshape(-1, 1)
    targets_1 = array[signal_start:signal_time]
    targets_2 = array[signal_time:signal_end]

    # Fit the two models to the two parts of the search region and get the
    # slope of the model
    if len(predictors_1) > 2:
        model_1 = LinearRegression().fit(predictors_1, targets_1)
        slope_1 = model_1.coef_[0]
    else:
        slope_1 = 1
    if len(predictors_2) > 2:
        model_2 = LinearRegression().fit(predictors_2, targets_2)
        slope_2 = model_2.coef_[0]
    else:
        slope_2 = -1

    return bool(slope_1 > 0 > slope_2)


def get_consistency_check_data(
    position: Tuple[int, int],
    signal_time: int,
    parang: np.ndarray,
    frame_size: Tuple[int, int],
    psf_diameter: float,
    n_test_positions: int = 5,
) -> List[Tuple[Tuple[int, int], int, np.ndarray]]:
    """
    Given a (spatial) `position` and a (temporal) `signal_time`,
    construct the planet path that is implied by these values and
    return `n_test_positions` new positions on that arc with the
    respective expected temporal signal position at these positions.

    Args:
        position:
        signal_time:
        parang:
        frame_size:
        psf_diameter:
        n_test_positions:

    Returns:

    """

    # Define useful shortcuts
    n_frames = len(parang)
    center = (frame_size[0] / 2, frame_size[1] / 2)

    # Assuming that the peak of the signal is at pixel `position` at the time
    # t=`signal_time`, use our knowledge about the movement of the planet
    # to compute the (spatial) position of the planet at point t=0.
    starting_position = rotate_position(
        position=position,
        center=center,
        angle=-float(parang[signal_time] - parang[0]),
    )

    # Create `n_test_times` (uniformly distributed) points in time at which
    # we check if the find a planet signal consistent with the above hypothesis
    test_times = np.linspace(0, n_frames - 1, n_test_positions)
    test_times = test_times.astype(int)

    # Loop over all test positions and get the expected position (both the peak
    # position and temporal region that is covered by the signal) of the signal
    results = list()
    for test_time in test_times:

        # Find the expected (spatial) position
        test_position = rotate_position(
            position=starting_position,
            center=center,
            angle=float(parang[test_time] - parang[0]),
        )

        # Round to the closest pixel position
        test_position = (int(test_position[0]), int(test_position[1]))

        # Get the expected signal length at this position
        length_1, length_2 = get_signal_length(
            position=test_position,
            signal_time=test_time,
            center=center,
            parang=parang,
            psf_diameter=psf_diameter,
        )

        # Initialize expected_mask as all False
        test_mask = np.zeros(n_frames).astype(bool)

        # Now add a block of 1s (that matches the expected signal length) to
        # the apply_idx centered on the current signal position
        time_1 = max(0, test_time - length_1)
        time_2 = min(n_frames, test_time + length_2)
        test_mask[time_1:test_time] = True
        test_mask[test_time:time_2] = True

        # Collect and store result tuple
        result = (test_position, test_time, test_mask)
        results.append(result)

    return results


def get_baseline_results(
    position: Tuple[int, int],
    stack: np.ndarray,
    obscon_array: np.ndarray,
    selection_mask: np.ndarray,
    get_model_instance: Callable,
    n_splits: int,
) -> Dict[str, np.ndarray]:
    """
    Get the baseline results for a given pixel, that is, the results
    without masking out any potential signal region.

    Args:
        position:
        stack:
        obscon_array:
        selection_mask:
        get_model_instance:
        n_splits:

    Returns:

    """

    # Select the full targets and predictors for the current position
    # using the given selection mask
    full_predictors = stack[:, selection_mask]
    full_targets = stack[:, position[0], position[1]].reshape(-1, 1)

    # Add observing conditions to the predictors
    full_predictors = np.hstack((full_predictors, obscon_array))

    # Create splitter for indices
    splitter = TrainTestSplitter(n_splits=n_splits, split_type='even_odd')

    # Prepare array for predictions
    full_predictions = np.full(len(full_targets), np.nan)

    # Loop over splits
    for train_idx, apply_idx in splitter.split(len(full_targets)):

        # Apply a scaler to the predictors and targets
        predictors_scaler = StandardScaler()
        train_predictors = predictors_scaler.fit_transform(
            full_predictors[train_idx]
        )
        apply_predictors = predictors_scaler.transform(
            full_predictors[apply_idx]
        )
        targets_scaler = StandardScaler()
        train_targets = targets_scaler.fit_transform(full_targets[train_idx])

        # Instantiate a new model
        model = get_model_instance()

        # Fit the model to the data
        model.fit(train_predictors, train_targets)

        # Get the model predictions
        predictions = model.predict(apply_predictors)

        # Undo the normalization
        predictions = targets_scaler.inverse_transform(predictions).ravel()

        # Store the result
        full_predictions[apply_idx] = predictions

    # Compute full residuals
    full_residuals = full_targets.ravel() - full_predictions.ravel()

    return dict(predictions=full_predictions, residuals=full_residuals)


def get_signal_masking_results(
    position: Tuple[int, int],
    stack: np.ndarray,
    parang: np.ndarray,
    obscon_array: np.ndarray,
    selection_mask: np.ndarray,
    get_model_instance: Callable,
    n_signal_times: int,
    frame_size: Tuple[int, int],
    psf_diameter: float,
    n_splits: int,
    max_signal_length: float,
) -> Dict[str, Dict[str, np.ndarray]]:
    """
    Get the results based on signal masking for given pixel: For
    `n_signal_times` points in time, compute the expected length (in
    time) of a planet signal here, mask out the corresponding temporal
    region and train a model on the rest of the time series.

    Args:
        position:
        stack:
        parang:
        obscon_array:
        selection_mask:
        get_model_instance:
        n_signal_times:
        frame_size:
        psf_diameter:
        n_splits:
        max_signal_length:

    Returns:

    """

    # Select the full targets and predictors for the current position
    # using the given selection mask
    full_predictors = stack[:, selection_mask]
    full_targets = stack[:, position[0], position[1]].reshape(-1, 1)

    # Add observing conditions to the predictors
    full_predictors = np.hstack((full_predictors, obscon_array))

    # Initialize results dictionary
    results: Dict[str, Dict[str, Union[np.ndarray, int]]] = dict()

    # Initialize metrics for finding "best" split
    best_mean = -np.infty
    best_predictions = np.full(len(full_targets), np.nan)
    best_residuals = np.full(len(full_targets), np.nan)
    best_signal_mask = np.full(len(full_targets), np.nan)
    best_signal_time = np.nan

    # Create splitter for indices
    splitter = TrainTestSplitter(n_splits=n_splits, split_type='even_odd')

    # Prepare array for predictions
    full_predictions = np.full(len(full_targets), np.nan)

    # Loop over different possible planet times and exclude them from the
    # training data to find the "best" exclusion region, and thus the best
    # model, which (ideally) was trained only on the part of the time series
    # that does not contain any planet signal.
    for i, (noise_mask, signal_mask, signal_time) in enumerate(
        get_noise_signal_masks(
            position=position,
            parang=parang,
            n_signal_times=n_signal_times,
            frame_size=frame_size,
            psf_diameter=psf_diameter,
            max_signal_length=max_signal_length,
        )
    ):

        # Add sub-dictionary in results
        results[str(i)] = dict(
            signal_mask=signal_mask, signal_time=signal_time
        )

        # Loop over cross-validation splits to avoid overfitting
        for train_idx, apply_idx in splitter.split(len(full_targets)):

            # Construct binary versions of the train_idx and apply_idx and
            # mask out the signal region from both of them
            binary_train_idx = np.full(len(noise_mask), False)
            binary_train_idx[train_idx] = True
            binary_train_idx[signal_mask] = False
            binary_apply_idx = np.full(len(noise_mask), False)
            binary_apply_idx[apply_idx] = True
            binary_apply_idx[signal_mask] = False

            # Select predictors and targets for training: Choose the training
            # positions without the presumed signal region
            train_predictors = full_predictors[binary_train_idx]
            train_targets = full_targets[binary_train_idx]

            # Apply a scaler to the predictors
            predictors_scaler = StandardScaler()
            train_predictors = predictors_scaler.fit_transform(
                train_predictors
            )
            full_predictors_transformed = predictors_scaler.transform(
                full_predictors
            )

            # Apply a predictor to the targets
            targets_scaler = StandardScaler()
            train_targets = targets_scaler.fit_transform(train_targets)

            # Instantiate a new model for learning the noise
            model = get_model_instance()

            # Fit the model to the training data
            model.fit(X=train_predictors, y=train_targets.ravel())

            # Get the predictions for every point in time using the current
            # model, and undo the target normalization
            predictions = model.predict(X=full_predictors_transformed)
            predictions = targets_scaler.inverse_transform(
                predictions.reshape(-1, 1)
            )

            # Select the predictions on the "apply region" (including the
            # signal region) and store them at the right positions
            full_predictions[apply_idx] = predictions[apply_idx].ravel()

            # Check for overfitting: If the standard deviation of the residuals
            # in the train region is much smaller than the standard deviation
            # of the residuals in the apply region (without the signal region),
            # then this could be an indication that our model is memorizing the
            # training data.
            train_residuals = (full_targets - predictions)[binary_train_idx]
            apply_residuals = (full_targets - predictions)[binary_apply_idx]
            if 3 * np.std(train_residuals) < np.std(apply_residuals):
                print(f'WARNING: Seeing signs of overfitting at {position}!')

        # Compute the full residuals
        full_residuals = full_targets.ravel() - full_predictions.ravel()

        # Add predictions and residuals to results dictionary
        results[str(i)]['predictions'] = full_predictions.ravel()
        results[str(i)]['residuals'] = full_residuals

        # Update our best guess for the planet position:
        # Choose the "best" exclusion region based on the idea that the planet
        # region, when not included in the training data, should have a higher
        # average than the rest of the time series (it's a positive bump), and
        # should exhibit a bump-like structure.
        # We then simply pick the highest such bump here. This is by no means
        # guaranteed to be ideal, but at least it is simple and fast...
        current_mean = np.mean(full_residuals[signal_mask])
        if current_mean > best_mean:
            if has_bump(full_residuals, signal_mask, signal_time):
                best_mean = current_mean
                best_signal_mask = signal_mask
                best_signal_time = signal_time
                best_predictions = full_predictions
                best_residuals = full_residuals

    # Add the (final) best predictions and residuals to results dict
    results['best'] = dict(
        signal_mask=best_signal_mask,
        signal_time=best_signal_time,
        predictions=best_predictions,
        residuals=best_residuals,
    )

    return results


def get_match_fraction(
    results: dict,
    parang: np.ndarray,
    psf_diameter: float,
    roi_mask: np.ndarray,
    n_test_positions: int,
) -> np.ndarray:
    """
    Run consistency tests to compute the fraction of matches for each
    position. This is the basis for choosing for which pixels we use the
    baseline model and for which we use the best signal masking model.

    Args:
        results:
        parang:
        psf_diameter:
        roi_mask:
        n_test_positions:

    Returns:

    """

    # Define some useful shortcuts
    signal_times = results['signal_times']
    frame_size = roi_mask.shape

    # Initialize array in which we keep track of the fraction of test positions
    # which are consistent with the best signal masking-model for each pixel
    match_fraction = np.full(frame_size, np.nan)

    # Loop over all positions in the ROI
    for position in tqdm(get_positions_from_mask(roi_mask), ncols=80):

        # Check if the best signal time for this pixel is NaN, which implies
        # that for this pixel there exists no best signal-masking model. In
        # this case, we do not have to run consistency checks and can set the
        # match fraction directly to 0.
        signal_time = results['best']['signal_time'][position[0], position[1]]
        if np.isnan(signal_time):
            match_fraction[position[0], position[1]] = 0
            continue

        # Otherwise, we can convert the best signal time to an integer
        signal_time = int(signal_time)

        # Get the data for the consistency check, that is, a list of spatial
        # positions and temporal indices (plus masks) at which we also expect
        # to see a planet if the hypothesis about the planet path that is
        # implied by the above best signal masking-model is correct.
        consistency_check_data = get_consistency_check_data(
            position=position,
            signal_time=signal_time,
            parang=parang,
            frame_size=frame_size,
            psf_diameter=psf_diameter,
            n_test_positions=n_test_positions,
        )

        # Initialize a list to keep track of the matching test position
        matches: List[int] = list()

        # Loop over all test positions and perform the consistency check
        for (test_position, test_time, test_mask) in consistency_check_data:

            # Get the number of the best-matching model, that is, the number
            # of the model whose signal mask is most similar to the test_mask
            split_number = np.abs(signal_times.ravel() - test_time).argmin()

            # Select the residuals from this model
            residuals = results[str(split_number)]['residuals'][
                :, test_position[0], test_position[1]
            ]

            # Check if the residuals contain NaN, which would indicate that
            # the test position is outside of the ROI, or was so close to the
            # star that the `max_signal_length` threshold has caused us not to
            # learn this model. In this case, we skip the test position instead
            # of recording a 0 in the matches, because we actually do not know
            # if this test position matches the original planet hypothesis.
            if np.isnan(residuals).any():
                continue

            # Split the residual into the part that should contain a planet
            # signal and a part that should only contain residual noise
            signal_residual = residuals[test_mask]
            noise_residual = residuals[~test_mask]

            # This is the centerpiece of our consistency check: We define our
            # criteria for counting a match:
            # 1. Do the residuals have a bump at the expected position?
            # 2. Is the mean of the (expected) signal part of the residual
            #    greater than than of the (expected) noise part?
            criterion_1 = has_bump(residuals, test_mask, test_time)
            criterion_2 = np.mean(signal_residual) > np.mean(noise_residual)

            # Only if all criteria are True do we count the current test
            # position as a match for the consistency check
            if criterion_1 and criterion_2:
                matches.append(1)
            else:
                matches.append(0)

        # Finally, compute the match fraction for the current position.
        # In case the match list is empty (i.e., we have skipped all test
        # positions), we manually set the match_fraction to 0.
        if matches:
            match_fraction[position[0], position[1]] = np.mean(matches)
        else:
            match_fraction[position[0], position[1]] = 0

    return match_fraction
