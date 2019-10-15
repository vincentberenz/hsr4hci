"""
Provide a half-sibling regression (HSR) model.
"""

# -----------------------------------------------------------------------------
# IMPORTS
# -----------------------------------------------------------------------------

from copy import deepcopy
from typing import Optional, Tuple, Union

from sklearn.decomposition import PCA
from tqdm import tqdm

import numpy as np

from hsr4hci.utils.forward_modeling import crop_psf_template, \
    get_signal_stack, get_collection_region_mask
from hsr4hci.utils.masking import get_positions_from_mask
from hsr4hci.utils.model_loading import get_class_by_name
from hsr4hci.utils.predictor_selection import get_predictor_mask
from hsr4hci.utils.roi_selection import get_roi_mask


# -----------------------------------------------------------------------------
# CLASS DEFINITIONS
# -----------------------------------------------------------------------------

class HalfSiblingRegression:
    """
    Wrapper class for a half-sibling regression model.

    This class essentially encapsulates the "outer loop", that is,
    looping over every pixel in the (spatial) region of interest and
    learning a model (or a collection of models) for it.

    Args:
        config: A dictionary containing the experiment configuration.
    """

    def __init__(self, config: dict):

        # Store the constructor arguments
        self.m__config = config

        # Define useful shortcuts
        self.m__config_model = config['experiment']['model']
        self.m__config_psf_template = config['experiment']['psf_template']
        self.m__config_sources = config['experiment']['sources']
        self.m__experiment_dir = config['experiment_dir']
        self.m__frame_size = config['dataset']['frame_size']
        self.m__lambda_over_d = config['dataset']['lambda_over_d']
        self.m__pixscale = config['dataset']['pixscale']
        self.m__use_forward_model = config['experiment']['use_forward_model']

        # Compute implicitly defined class variables
        roi_ier = config['experiment']['roi']['inner_exclusion_radius']
        roi_oer = config['experiment']['roi']['outer_exclusion_radius']
        self.m__roi_mask = get_roi_mask(mask_size=self.m__frame_size,
                                        pixscale=self.m__pixscale,
                                        inner_exclusion_radius=roi_ier,
                                        outer_exclusion_radius=roi_oer)

        # Initialize additional class variables
        self.m__collections = dict()
        self.m__sources = dict()

    def get_cropped_psf_template(self,
                                 psf_template: np.ndarray) -> np.ndarray:
        """
        Crop a given `psf_template` according to the options specified
        in the experiment configuration.

        Args:
            psf_template: A 2D numpy array containing the raw,
                unsaturated PSF template.

        Returns:
            A 2D numpy array containing the cropped and masked PSF
            template (according to the experiment config).
        """

        # Crop and return the PSF template
        crop_psf_template_arguments = \
            {'psf_template': psf_template,
             'psf_radius': self.m__config_psf_template['psf_radius'],
             'rescale_psf': self.m__config_psf_template['rescale_psf'],
             'pixscale': self.m__pixscale,
             'lambda_over_d': self.m__lambda_over_d}
        return crop_psf_template(**crop_psf_template_arguments)

    def get_coefficients(self) -> np.ndarray:
        """
        Get all planet coefficients for all spatial positions.

        Returns:
            A numpy arrays containing the planet coefficients. The array
            has shape (max_size, width, height), where width and height
            refer to the spatial size of the stack on which the model
            was trained, and max_size is the number of pixels / models
            in the largest collection region.
            For all positions where the respective collection contains
            less pixels than max_size, the remaining array entries are
            filled with NaN; the same holds for all positions for which
            no collection was trained in the first place.
            This array may be useful to experiment with the way the
            detection map is computed. For example, a straightforward
            and simple way to obtain a detection map is to take the
            nanmedian along the first axis.
        """

        # Initialize dictionary to temporary hold the coefficients we collect
        tmp_coefficients = dict()

        # Keep track of the largest number of coefficients in a collection
        # (the size of a collection depends on its position)
        max_size = 0

        # Loop over all collections
        for position, collection in self.m__collections.items():

            # Loop over collection and collect the planet coefficients
            collection_coefficients = list()
            for _, predictor in collection.m__predictors.items():
                coefficient = predictor.get_signal_coef()
                collection_coefficients.append(coefficient)

            # Store them and update the maximum number of coefficients
            tmp_coefficients[position] = collection_coefficients
            max_size = max(max_size, len(collection_coefficients))

        # Define the shape of the output array and initialize it with NaNs
        output_shape = (max_size, ) + tuple(self.m__frame_size)
        coefficients = np.full(output_shape, np.nan).astype(np.float32)

        # Convert the dictionary of coefficients into an array
        for position, position_coefficients in tmp_coefficients.items():
            n_entries = len(position_coefficients)
            coefficients[:n_entries, position[0], position[1]] = \
                position_coefficients

        return coefficients

    def get_noise_predictions(self,
                              stack_or_shape: Union[np.ndarray, tuple]
                              ) -> np.ndarray:
        """
        Get the predictions of the noise part of the models we learned.

        Args:
            stack_or_shape: Either a 3D numpy array of shape (n_frames,
                width, height) containing a stack of frames on which
                the trained models should be evaluated, or just a tuple
                (n_frames, width, height) containing the shape of the
                original stack on which the data was trained. In the
                first case, we will compute the PCA on the new stack
                and apply the trained models on them to obtain a stack
                of predictions. In the second case, we only return the
                predictions of the models on the data that they were
                trained on (in which case we do not need to compute the
                PCA for the model inputs again).

        Returns:
            A 3D numpy array with the same shape as `stack_or_shape`
            that contains, at each position (x, y) in the region of
            interest, the prediction of the model for (x, y). The model
            to make the prediction is taken from the collection at the
            same position. For positions for which no model was trained,
            the prediction default to NaN (i.e., you might want to use
            np.nan_to_num() before subtracting the predictions from
            your data to get the residuals of the model).
        """

        # Define stack shape based on whether we have received a stack or
        # only the shape of the stack
        if isinstance(stack_or_shape, tuple):
            stack_shape = stack_or_shape
        else:
            stack_shape = stack_or_shape.shape

        # Initialize an array that will hold our predictions
        predictions = np.full(stack_shape, np.nan).astype(np.float32)

        # Loop over all positions in the ROI and the respective collections
        for position, collection in \
                tqdm(self.m__collections.items(), ncols=80):

            # Get a copy of the predictor for this position
            predictor = deepcopy(collection.m__predictors[position].m__model)

            # If we have trained the model with forward modeling, drop the
            # signal part of the model (we're only predicting the noise here)
            if self.m__use_forward_model:
                predictor.coef_ = predictor.coef_[:-1]

            # If necessary, pre-compute PCA on stack to build sources
            if isinstance(stack_or_shape, tuple):
                sources = self.m__sources[position]
            else:
                sources = self.precompute_pca(stack=stack_or_shape,
                                              position=position)

            # Make prediction for position and store in predictions array
            predictions[:, position[0], position[1]] = \
                predictor.predict(X=sources)

        return predictions

    def get_best_fit_planet_model(self,
                                  detection_map: np.ndarray,
                                  stack_shape: Tuple[int, int, int],
                                  parang: np.ndarray,
                                  psf_template: np.ndarray) -> np.ndarray:
        """
        Get the best fit planet model (BFPM).

        Args:
            detection_map: A 2D numpy array containing the detection map
                obtained with get_detection_map().
            stack_shape: A tuple containing the shape of the stack on
                which we trained the model, which will also be the shape
                of the array containing the best fit planet model.
            parang: A 1D numpy array containing the parallactic angles.
            psf_template: A 2D numpy array containing the unsaturated
                PSF template (raw, i.e., not cropped or masked).

        Returns:
            A 3D numpy array with the same shape as `stack` that
            contains our best fit planet model.
        """

        # Crop and mask the PSF template
        psf_cropped = self.get_cropped_psf_template(psf_template=psf_template)

        # Initialize the best fit planet model
        best_fit_planet_model = np.zeros(stack_shape).astype(np.float32)

        # Get positions where detection map is positive (we ignore negative
        # entries because they are do not make sense astrophysically)
        positive_pixels = \
            get_positions_from_mask(np.nan_to_num(detection_map) > 0)

        # Loop over all these positions
        for position in tqdm(positive_pixels, ncols=80):

            # Compute the weight according to the detection map
            factor = detection_map[position[0], position[1]]

            # Compute the forward model for this position
            signal_stack = get_signal_stack(position=position,
                                            frame_size=self.m__frame_size,
                                            parang=parang,
                                            psf_cropped=psf_cropped)

            # Add the forward model for this position to the best fit model
            best_fit_planet_model += factor * signal_stack

        # "Normalize" the best-fit planet model
        best_fit_planet_model /= np.max(best_fit_planet_model)
        best_fit_planet_model *= np.nanmax(detection_map)

        return best_fit_planet_model

    def get_detection_map(self) -> np.ndarray:
        """
        Collect the detection map for the model.

        A detection map contains, at each position (x, y) within the
        region of interest, the average planet coefficient, where the
        average is taken over all models that belong to the collection
        (i.e., the "sausage-shaped" planet trace region) for (x, y).
        By default, the median is used to average the coefficients.
        In case we trained the model with use_forward_model=False, the
        detection map is necessarily empty (because the model does not
        contain a coefficient for the planet signal).

        Returns:
            A 2D numpy array containing the detection map for the model.
        """

        # Initialize an empty detection map
        detection_map = np.full(self.m__frame_size, np.nan).astype(np.float32)

        # If we are not using a forward model, we obviously cannot compute a
        # detection map, hence we return an empty detection map
        if not self.m__use_forward_model:
            print('\nWARNING: You called get_detection_map() with '
                  'use_forward_model=False! Returned an empty detection map.')
            return detection_map

        # Otherwise, we can loop over all collections and collect the
        # coefficients corresponding to the planet part of the model
        for position, collection in self.m__collections.items():
            detection_map[position] = collection.get_average_signal_coef()

        return detection_map

    def precompute_pca(self,
                       stack: np.ndarray,
                       position: Tuple[int, int]) -> np.ndarray:
        """
        Precompute the PCA for a given position (i.e., a single pixel).

        Args:
            stack: A 3D numpy array of shape (n_frames, width, height)
                containing the stack of frames to train on.
            position: A tuple (x, y) containing the position for which
                to pre-compute the PCA.

        Returns:
            The `sources` for the given position.
        """

        # Define some shortcuts
        n_components = self.m__config_sources['pca_components']
        pca_mode = self.m__config_sources['pca_mode']
        sv_power = self.m__config_sources['sv_power']
        mask_type = self.m__config_sources['mask']['type']
        mask_params = self.m__config_sources['mask']['parameters']

        # Collect options for mask creation
        mask_args = dict(mask_size=self.m__frame_size,
                         position=position,
                         mask_params=mask_params,
                         lambda_over_d=self.m__lambda_over_d,
                         pixscale=self.m__pixscale)

        # Get predictor pixels ("sources", as opposed to "targets")
        predictor_mask = get_predictor_mask(mask_type=mask_type,
                                            mask_args=mask_args)
        sources = stack[:, predictor_mask].astype(np.float32)

        # Set up the principal component analysis (PCA)
        pca = PCA(n_components=n_components)

        # Depending on the pca_mode, we either use the PCs directly...
        if pca_mode == 'fit':

            # Fit the PCA to the data. We take the transpose of the sources
            # such that the  principal components found by the PCA are also
            # time series.
            pca.fit(X=sources.T)

            # Select the principal components, undo the transposition, and
            # multiply the them with the desired power of the singular values
            tmp_sources = pca.components_.T
            tmp_sources *= np.power(pca.singular_values_, sv_power)

        # ...or the original data projected onto the PCs
        elif pca_mode == 'fit_transform':

            # Fit the PCA, transform the data into the rotated coordinate
            # system, and then multiply with the desired power of the singular
            # values. This is equivalent to first multiplying the PCs with the
            # SVs and then projecting; however, fit_transform() is generally
            # more efficient.
            tmp_sources = pca.fit_transform(X=sources)
            tmp_sources *= np.power(pca.singular_values_, sv_power)

        else:
            raise ValueError('pca_mode must be one of the following: '
                             '"fit" or "fit_transform"!')

        return tmp_sources.astype(np.float32)

    def train(self,
              stack: np.ndarray,
              parang: Optional[np.ndarray],
              psf_template: Optional[np.ndarray]):
        """
        Train the complete HSR model.

        This function is essentially only a loop over all functions in
        the region of interest; the actual training at each position
        happens in train_position().

        Args:
            stack: A 3D numpy array of shape (n_frames, width, height)
                containing the stack of frames to train on.
            parang: A numpy array of length n_frames containing the
                parallactic angle for each frame in the stack.
            psf_template: A 2D numpy array containing the unsaturated
                PSF template which is used for forward modeling of the
                planet signal. If None is given instead, no forward
                modeling is performed.
        """

        # Crop the PSF template to the size specified in the config
        psf_cropped = self.get_cropped_psf_template(psf_template=psf_template)

        # Get positions of pixels in ROI
        roi_pixels = get_positions_from_mask(self.m__roi_mask)

        # Run training by looping over the ROI and calling train_position()
        for position in tqdm(roi_pixels, total=len(roi_pixels), ncols=80):
            self.train_position(position=position,
                                stack=stack,
                                parang=parang,
                                psf_cropped=psf_cropped)

    def train_position(self,
                       position: Tuple[int, int],
                       stack: np.ndarray,
                       parang: np.ndarray,
                       psf_cropped: np.ndarray):
        """
        Train the models for a given `position`.

        Essentially, this function sets up a PixelPredictorCollection
        and trains it. The motivation for separating this into its own
        function was to simplify parallelization of the training on a
        batch queue based cluster (where every position could be
        trained independently in a separate job).

        Args:
            position: A tuple (x, y) containing the position for which
                to train a collection. Note: This corresponds to the
                position where the planet in the forward model will be
                placed at t=0, that is, in the first frame.
            stack: A 3D numpy array of shape (n_frames, width, height)
                containing the training data.
            parang: A 1D numpy array of shape (n_frames,) containing
                the corresponding parallactic angles for the stack.
            psf_cropped: A 2D numpy containing the cropped and masked
                PSF template that will be used to compute the forward
                model.
        """

        # Create a PixelPredictorCollection for this position
        collection = PixelPredictorCollection(position=position,
                                              hsr_instance=self)

        # Train and save the collection for this position
        collection.train_collection(stack=stack,
                                    parang=parang,
                                    psf_cropped=psf_cropped)

        # Add to dictionary of trained collections
        self.m__collections[position] = collection


# -----------------------------------------------------------------------------


class PixelPredictorCollection:
    """
    Wrapper class around a collection of PixelPredictors.

    A collection consists of a collection region, which is given by the
    "sausage"-shaped trace of a planet in a forward model (or a single
    position, in case we are not using forward modeling), and a separate
    PixelPredictor instance for every position within this region.

    Quantities such as a detection map are then obtained by averaging
    the planet coefficient over all models in the the collection region.
    This is, in essence, a method to test if a suspected signal is
    consistent with the expected apparent motion that a real planet
    signal would exhibit in the data.

    Args:
        position: A tuple (x, y) containing the position for which
            to train a collection. Note: This corresponds to the
            position where the planet in the forward model will be
            placed at t=0, that is, in the first frame.
    """

    def __init__(self,
                 position: Tuple[int, int],
                 hsr_instance: HalfSiblingRegression):

        # Store the constructor arguments
        self.m__position = position
        self.m__hsr_instance = hsr_instance

        # Initialize additional class variables
        self.m__collection_region = None
        self.m__predictors = dict()
        self.m__collection_name = \
            f'collection_{self.m__position[0]}_{self.m__position[1]}'

        # Get variables which can be inherited from parent
        self.m__use_forward_model = hsr_instance.m__use_forward_model
        self.m__config_model = hsr_instance.m__config_model

    def get_average_signal_coef(self) -> Optional[float]:
        """
        Compute the average signal coefficient for this collection.

        Returns:
            The average (by default: the median) planet coefficient of
            all models in this collection. In case the collection was
            trained without forward modeling, None is returned.
        """

        # We can only compute an average signal coefficient if we have
        # trained the collection using forward modeling
        if self.m__use_forward_model:

            # Collect signal coefficients for all predictors in the collection
            signal_coefs = [predictor.get_signal_coef() for _, predictor
                            in self.m__predictors.items()]

            # Return the median of all signal coefficients in the collection
            return float(np.nanmedian(signal_coefs))

        # Otherwise, we just return None
        return None

    def train_collection(self,
                         stack: np.ndarray,
                         parang: np.ndarray,
                         psf_cropped: np.ndarray):
        """
        Train this collection.

        This function essentially contains a loop over all positions in
        the collection, for which a PixelPredictor is initialized and
        trained.

        Args:
            stack: A 3D numpy array of shape (n_frames, width, height)
                containing the training data.
            parang: A 1D numpy array of shape (n_frames,) containing the
                corresponding parallactic angles for the stack.
            psf_cropped: A 2D numpy containing the cropped and masked
                PSF template that will be used to compute the forward
                model.
        """

        # ---------------------------------------------------------------------
        # Get signal_stack and collection_region based on use_forward_model
        # ---------------------------------------------------------------------

        if self.m__use_forward_model:
            signal_stack = get_signal_stack(position=self.m__position,
                                            frame_size=stack.shape[1:],
                                            parang=parang,
                                            psf_cropped=psf_cropped)
            collection_region_mask = get_collection_region_mask(signal_stack)
            self.m__collection_region = \
                get_positions_from_mask(collection_region_mask)
        else:
            signal_stack = None
            self.m__collection_region = [self.m__position]

        # ---------------------------------------------------------------------
        # Loop over all positions in the collection region
        # ---------------------------------------------------------------------

        for position in self.m__collection_region:

            # -----------------------------------------------------------------
            # If necessary, pre-compute the sources for this position
            # -----------------------------------------------------------------

            # If the sources dictionary of the HSR instance does not contain
            # the current position, we need to pre-compute the PCA for it
            if position not in self.m__hsr_instance.m__sources.keys():
                sources = \
                    self.m__hsr_instance.precompute_pca(stack=stack,
                                                        position=position)
                self.m__hsr_instance.m__sources[position] = sources

            # Otherwise we can simply retrieve the sources from this dict
            else:
                sources = self.m__hsr_instance.m__sources[position]

            # -----------------------------------------------------------------
            # Collect the targets and, if needed, the forward model
            # -----------------------------------------------------------------

            # Get regression target
            targets = stack[:, position[0], position[1]]

            # Get planet signal (only if we are using a forward model)
            if self.m__use_forward_model:
                planet_signal = signal_stack[:, position[0], position[1]]
            else:
                planet_signal = None

            # -----------------------------------------------------------------
            # Create a new PixelPredictor, train it, and store it
            # -----------------------------------------------------------------

            # Create a new PixelPredictor instance for this position
            pixel_predictor = PixelPredictor(collection_instance=self)

            # Train pixel predictor for the selected sources and targets. The
            # augmentation of the sources with the planet_signal (in case it
            # is not None) happens automatically inside the PixelPredictor.
            pixel_predictor.train(sources=sources,
                                  targets=targets,
                                  planet_signal=planet_signal)

            # Add trained PixelPredictor to PixelPredictorCollection
            self.m__predictors[position] = pixel_predictor


# -----------------------------------------------------------------------------


class PixelPredictor:
    """
    Wrapper class for a predictor model of a single pixel.

    Args:
        collection_instance: Reference to the PixelPredictorCollection
            instance to which this PixelPredictor belongs.
    """

    def __init__(self, collection_instance: PixelPredictorCollection):

        # Store constructor arguments
        self.m__collection_instance = collection_instance

        # Initialize additional class variables
        self.m__model = None

        # Get variables which can be inherited from parents
        self.m__use_forward_model = collection_instance.m__use_forward_model
        self.m__config_model = collection_instance.m__config_model

    def get_signal_coef(self):
        """
        Get the coefficient for the planet signal for this predictor.

        Returns:
            The coefficient for the planet signal for this predictor.
        """

        if self.m__use_forward_model and self.m__model is not None:
            return self.m__model.coef_[-1]
        return None

    def train(self,
              sources: np.ndarray,
              targets: np.ndarray,
              planet_signal: Optional[np.ndarray] = None):
        """
        Train the model wrapper by the PixelPredictor.

        Args:
            sources: A 2D numpy array of shape (n_samples, n_features),
                which contains the training data (also known as the
                "independent variables") for the model.
            targets: A 1D numpy array of shape (n_samples,) that
                contains the regression targets (i.e, the "dependent
                variable") of the fit.
            planet_signal: A 1D numpy array containing the planet signal
                time series (from forward modeling) to be included in the
                model. May be None if `use_forward_model` is False.
        """

        # Instantiate a new model according to the model_config
        model_class = \
            get_class_by_name(module_name=self.m__config_model['module'],
                              class_name=self.m__config_model['class'])
        self.m__model = model_class(**self.m__config_model['parameters'])

        # Augment the sources: if we are using a forward model, we need to
        # add the planet signal as a new column to the sources here; if not,
        # we leave the sources unchanged
        if self.m__use_forward_model:

            # Sanity check: Make sure we actually got a planet signal
            if planet_signal is None:
                raise RuntimeError('use_forward_model is True, but no planet'
                                   'signal from forward modeling was provided')

            # Augment the sources by adding the planet signal as a new column
            sources = np.column_stack([sources, planet_signal.reshape(-1, 1)])

        # Fit model to the training data
        self.m__model.fit(X=sources, y=targets)

    def predict(self, sources: np.ndarray) -> np.ndarray:
        """
        Make predictions for given sources.

        Args:
            sources: A 2D numpy array of shape (n_samples, n_features),
                which contains the data for which we want to make a
                prediction using the trained model.

        Returns:
            A 1D numpy array of shape (n_samples, ) containing the
            model predictions for the given inputs (sources).
        """

        if self.m__model is not None:
            return self.m__model.predict(X=sources)
        raise RuntimeError('You called predict() on an untrained model!')
