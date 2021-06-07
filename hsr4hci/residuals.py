"""
Utility functions that are related to dealing with residuals.
"""

# -----------------------------------------------------------------------------
# IMPORTS
# -----------------------------------------------------------------------------

from itertools import product
from math import fmod
from typing import Dict, List, Tuple, Union

from photutils.centroids import centroid_com
from polarTransform import convertToPolarImage
from skimage.feature import blob_log, match_template
from skimage.filters import gaussian

import h5py
import numpy as np

from hsr4hci.coordinates import get_center
from hsr4hci.general import shift_image, crop_or_pad
from hsr4hci.photometry import get_flux


# -----------------------------------------------------------------------------
# FUNCTION DEFINITIONS
# -----------------------------------------------------------------------------

def assemble_residual_stack_from_hypotheses(
    hypotheses: np.ndarray,
    selection_mask: np.ndarray,
    residuals: Union[h5py.File, Dict[str, np.ndarray]],
) -> np.ndarray:
    """
    Assemble the residual stack based on the `selection_mask` and the
    `hypotheses`: For each spatial pixel where the `selection_mask` is
    True, use the residual from the model given by the respective entry
    in `hypotheses`; for all other pixels, use the "default" residual.

    Args:
        hypotheses: A 2D numpy array of shape `(x_size, y_size)`. Each
            position contains an integers which represents the signal
            time which appears to be the best hypothesis for this pixel
            (i.e., "if there ever is a planet in this pixel, it should
            be at this time").
        selection_mask: A 2D numpy array of shape `(x_size, y_size)`
            which contains a mask that determines for which pixels the
            default residual is used and for which pixel the residual
            based on signal fitting / masking is used.
        residuals: The dictionary, or an open HDF file, which contains
            the full results from training both the "default" and the
            models based on signal fitting / masking.

    Returns:
        A 3D numpy array (whose shape matches the stack) containing the
        "best" residuals based on the given the `hypotheses`.
    """

    # Initialize the result as the default residuals
    result = np.array(residuals['default'])
    n_frames, x_size, y_size = result.shape

    # Loop over all spatial positions and pick the signal masking-based
    # residual based on the the respective hypothesis for the pixel
    for x, y in product(np.arange(x_size), np.arange(y_size)):

        # If we do not have a hypothesis, or the selection mask did not select
        # this pixel, do nothing (i.e., keep the "default" residual)
        if np.isnan(hypotheses[x, y]) or (not selection_mask[x, y]):
            continue

        # Otherwise (if the selection mask is True), replace the "default"
        # residual with the one that matches the hypothesis for this pixel
        else:
            signal_time = str(int(hypotheses[x, y]))
            result[:, x, y] = np.array(residuals[signal_time][:, x, y])

    return result


def _get_radial_gradient_mask(
    mask_size: Tuple[int, int], power: float = 0.5
) -> np.ndarray:
    """
    Compute radial gradient, that is, a array where the value is its
    separation from the center (to the power of `power`). This can be
    used to re-weight the match fraction to take into account that the
    "uncertainty" for pixels far from the center is smaller than for
    pixels close to the star.

    Args:
        mask_size: A tuple `(x_size, y_size)` specifying the size of
            the radial gradient mask.
        power: The power to which the gradient is taken (default: 0.5).

    Returns:
        A radial gradient mask of the given size.
    """

    sx, sy = mask_size
    x, y = np.ogrid[0:sx, 0:sy]
    r = np.hypot(x - sx / 2 + 0.5, y - sy / 2 + 0.5)

    return np.asarray(r ** power)


def _get_expected_signal(
    frame_size: Tuple[int, int],
    field_rotation: float,
    psf_template: np.ndarray,
    grid_size: int,
    relative_rho: float = 0.500,
    relative_phi: float = 0.275,
) -> np.ndarray:

    # Define shortcuts
    center = get_center(frame_size)
    rho = min(center[0], center[1]) * relative_rho
    phi = np.pi * (1 + relative_phi)

    # Resize the PSF template
    psf_resized = crop_or_pad(psf_template, frame_size)
    psf_resized /= np.max(psf_resized)
    psf_resized = np.asarray(np.clip(psf_resized, 0.2, None)) - 0.2
    psf_resized /= np.max(psf_resized)

    # Create the expected signal in Cartesian coordinates
    expected_signal_cartesian = np.zeros(frame_size)
    alpha = np.deg2rad(field_rotation / 2)
    for offset in np.linspace(-alpha, alpha, 2 * int(field_rotation)):
        x = rho * np.cos(phi + offset)
        y = rho * np.sin(phi + offset)
        shifted = shift_image(psf_resized, (x, y))
        expected_signal_cartesian += shifted

    # Convert the expected signal to polar coordinates
    expected_signal_polar, _ = convertToPolarImage(
        image=np.nan_to_num(expected_signal_cartesian),
        radiusSize=grid_size,
        angleSize=grid_size,
        initialRadius=0,
        finalRadius=min(center[0], center[1]),
    )
    expected_signal_polar = expected_signal_polar.T
    expected_signal_polar /= np.max(expected_signal_polar)

    # Make sure that the expected signal is properly centered
    polar_center = get_center(expected_signal_polar.shape)
    com = centroid_com(expected_signal_polar)
    d_rho = polar_center[0] - com[0]
    d_phi = polar_center[1] - com[1]
    expected_signal_polar = shift_image(expected_signal_polar, (d_rho, d_phi))

    return np.asarray(expected_signal_polar)


def _prune_blobs(blobs: List[Tuple[float, float, float]]) -> np.ndarray:
    """
    Prune a list of blobs: if there are two blobs at (approximately) the
    same separation, we only keep the brighter blob.

    Note: This is an extremely simple version of "pruning", and it does
    *not* cover all corner cases. However, it seems sufficient for most
    cases that will cause problems in practice.

    Args:
        blobs: A list of 3-tuples describing the blobs we want to prune,
            where each tuple has the form `(separation, polar angle,
            brightness)`.

    Returns:
        A 2D numpy array containing only the positions (separation and
        polar angle) of the pruned list of blobs.
    """

    # Store the pruned blobs that we will return
    pruned = []

    # Loop over all blobs to check which of them we will keep
    for i in range(len(blobs)):

        # Unpack the reference blob; this is the blob for which we
        # decide if we want to keep it or not
        rho_1, phi_1, brightness_1 = blobs[i]

        # Loop (again) over all blobs
        for j in range(len(blobs)):

            # We do not compare a blob with itself
            if i == j:
                continue

            # Unpack the blob with which we will compare
            rho_2, phi_2, brightness_2 = blobs[j]

            # If the blob that we are comparing with is radially close and
            # brighter than the reference blob, we break the inner loop
            if abs(rho_1 - rho_2) <= 3 and brightness_2 > brightness_1:
                break

        # Only if the inner for-loop terminated normally, that is, not via the
        # break command, do we add the reference blob
        else:
            pruned.append((rho_1, phi_1))

    return np.array(pruned)


def get_residual_selection_mask(
    match_fraction: np.ndarray,
    parang: np.ndarray,
    psf_template: np.ndarray,
    grid_size: int = 256,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    Based on the `match_fraction`, determine the `selection_mask`, that
    is, the mask that decides for which pixels we use the default model
    and for which we use the model based in signal fitting / masking.

    Ideally, it would be sufficient to simply threshold the match
    fraction to obtain the selection mask. In practice, however, this
    does not always work well.
    Therefore, this function uses the following, more complex heuristic:

        1. Convert the match fraction from Cartesian to polar
           coordinates (r, theta).
        2. In polar coordinates, the planet signal is translation
           invariant; and even more importantly, we know exactly how
           it should look like. We can, therefore, compute a cross-
           correlation with the expected signal.
        3. In the result of this template matching, we can find peaks,
           which correspond to the (r, theta) position of the planet
           signals in the match fraction.
        4. Based on these peaks, we explicitly construct the selection
           mask. These masks are more interpretable and will not contain
           any "random" pixels or shapes, unlike the masks that can be
           obtained using the simple thresholding approach.

    To further improve the result, some additional tricks (e.g., re-
    weighting of match fraction using a radial gradient) are used which
    have proven useful in preliminary experiments.

    Args:
        match_fraction: 2D numpy array containing the match fraction.
        parang: 1D numpy array containing the parallactic angles.
        psf_template: 2D numpy array containing the PSF template.
        grid_size: The size (width and height) of the match fraction
            when projecting to polar coordinates. This value should
            usually be chosen to be larger than the size of the MF in
            Cartesian coordinates (i.e, the usual frame size). Larger
            values can give better results, but slow things down.

    Returns:
        A 5-tuple of numpy array, containing:
        (1) The selection mask, that is, a 2D numpy array containing a
            binary mask that can be used to select the pixels for which
            the residuals based on signal fitting / signal masking
            should be used.
        (2) The polar projection, that is, a 2D numpy array of shape
            `(grid_size, grid_size)` containing the polar projection
            of the match fraction (mostly for debugging purposes).
        (3) The output of the template matching ("heatmaps"), which has
            the same shape as (3). This is the cross-correlation of
            the polar projection with the expected signal.
        (4) The expected signal, that is, a 2D numpy array of shape
            `(grid_size, grid_size)` that contains the (approximate)
            expected planet signal that we would hope to find in the
            polar projections.
        (5) The positions of the (centers) of the planet trace arcs,
            as found by the cross-correlation procedure, that is, a 2D
            numpy array of shape `(N, 2)` where `N` is the number of
            found planet traces. Each tuple consists of the separation
            and the polar angle (in radian).
    """

    # -------------------------------------------------------------------------
    # Preparations
    # -------------------------------------------------------------------------

    # Compute field rotation; check that it is physically meaningful
    field_rotation = abs(parang[-1] - parang[0])
    if field_rotation > 180:
        raise RuntimeError('field_rotation is greater than 180 degrees!')

    # Prepare the Cartesian match fraction; define shortcuts
    cartesian = np.copy(match_fraction)
    cartesian = np.nan_to_num(cartesian)
    x_size, y_size = cartesian.shape
    frame_size = (x_size, y_size)
    center = get_center(cartesian.shape)

    # Multiply the match fraction with a gradient mask that aims to (partially)
    # compensates for the fact that at small separations, only few pixels
    # contribute to the match fraction, meaning it is "easier" to get a
    # high match fraction at small separations, whereas for pixels at larger
    # separation, this "uncertainty" is smaller.
    cartesian *= _get_radial_gradient_mask(frame_size)

    # Applying a mild Gaussian blur filter appears to give better results
    # when projecting to polar coordinates
    cartesian = gaussian(cartesian, sigma=1)

    # -------------------------------------------------------------------------
    # Prepare the template of the expected signal for the cross-correlation
    # -------------------------------------------------------------------------

    # Compute the signal that we expect to see in polar coordinates.
    # Note: This signal is not perfectly translation invariant, so the signal
    # that we will be search for is approximately the median of the possible
    # signals that we would expect.
    expected_signal = _get_expected_signal(
        frame_size=frame_size,
        field_rotation=field_rotation,
        psf_template=psf_template,
        grid_size=grid_size,
    )

    # -------------------------------------------------------------------------
    # Project to polar coordinates
    # -------------------------------------------------------------------------

    # Project the match fraction from Cartesian to polar coordinates
    polar, _ = convertToPolarImage(
        image=np.nan_to_num(cartesian),
        radiusSize=grid_size,
        angleSize=grid_size,
        initialRadius=0,
        finalRadius=min(center[0], center[1]),
        initialAngle=-np.pi,
        finalAngle=np.pi,
    )
    polar = polar.T

    # This removes a lot of "background noise" from the polar match fraction
    # and yields cleaner planet signals that give better template matches
    polar *= np.std(polar, axis=1, keepdims=True)
    polar -= np.median(polar, axis=1, keepdims=True)

    # Normalize the polar match fraction
    polar /= np.max(polar)

    # -------------------------------------------------------------------------
    # Cross-correlate with expected signal
    # -------------------------------------------------------------------------

    # Cross-correlate the polar match fraction with the expected signal
    matched = match_template(
        image=polar,
        template=expected_signal,
        pad_input=True,
        mode='wrap',
    )
    # noinspection PyTypeChecker
    matched = np.clip(matched, a_min=0, a_max=None)

    # -------------------------------------------------------------------------
    # Run blob finder, and prune blobs
    # -------------------------------------------------------------------------

    # Loop over two offset for the global phase: The choice of the phase for
    # the polar representation is arbitrary, and if we are unlucky, we might
    # have a planet signal that is "split into two", that, half the signal is
    # on the left-hand edge of the polar representation, and the other half is
    # on the right-hand edge. Therefore, we run with two different phases.
    blobs: List[Tuple[float, float, float]] = []
    for global_phase_offset in (0, np.pi):

        # ---------------------------------------------------------------------
        # Run blob finder and construct signal mask
        # ---------------------------------------------------------------------

        # Apply the global phase shift to the outputs of the template matching
        ratio = global_phase_offset / (2 * np.pi)
        phase_shifted_matched = np.roll(
            matched, shift=int(grid_size * ratio), axis=1
        )

        # Find blobs / peaks in the matched filter result
        # Depending on the data set and / or the hyper-parameters of the HSR,
        # the parameters of the blob finder might require additional tuning.
        tmp_blobs = blob_log(
            image=phase_shifted_matched,
            max_sigma=grid_size / 4,
            num_sigma=32,
            threshold=0.05,
            overlap=0.0,
        )

        # Process blobs: First, drop all blobs that are too close to the image
        # border. Then replace the last column (by default the radius of the
        # blob) by the brightness of the blob, and finally, convert the first
        # two columns (the coordinates of the blob in the polar match fraction)
        # to values for the radius  and separation in the original (Cartesian)
        # match fraction.
        for i in range(len(tmp_blobs)):

            # Unpack blob coordinates
            rho, phi, _ = tmp_blobs[i]

            # Ignore blobs that are too close to the border
            if phi < grid_size / 4 or phi > 3 * grid_size / 4:
                continue

            # Measure blob brightness
            _, brightness = get_flux(
                frame=phase_shifted_matched, position=(rho, phi), mode='P'
            )

            # Adjust for coordinate system conventions / scaling
            rho *= min(center[0], center[1]) / grid_size
            phi = (2 * np.pi * phi / grid_size) + global_phase_offset + np.pi
            phi = fmod(phi, 2 * np.pi)

            blobs.append((rho, phi, brightness))

    # Prune the list of blobs: if there are two planet candidates at the same
    # separation, we should only keep the brighter one
    positions = _prune_blobs(blobs)

    # -------------------------------------------------------------------------
    # Finally, create the selection mask
    # -------------------------------------------------------------------------

    # Resize and normalize the PSF template
    psf_resized = crop_or_pad(psf_template, frame_size)
    psf_resized /= np.max(psf_resized)
    psf_resized = np.asarray(np.clip(psf_resized, 0.2, None)) - 0.2
    psf_resized /= np.max(psf_resized)

    # Initialize the selection mask
    selection_mask = np.zeros(frame_size)

    # Loop over blobs and create an arc at the position defined by (rho, phi):
    # place many shifted copies of the PSF template at the positions of the arc
    for rho, phi in positions:
        alpha = np.deg2rad(field_rotation / 2)
        for offset in np.linspace(-alpha, alpha, 2 * int(field_rotation)):
            x = rho * np.cos(phi + offset)
            y = rho * np.sin(phi + offset)
            shifted = shift_image(psf_resized, (x, y))
            selection_mask += shifted

    # Finally, threshold the selection mask (which so far is a sum of shifted
    # PSF templates) to binarize it
    selection_mask = selection_mask * cartesian > 0.2

    return selection_mask, polar, matched, expected_signal, positions
