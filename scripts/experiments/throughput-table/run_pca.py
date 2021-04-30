"""
Inject fake planet and run a PCA-based post-processing pipeline.
"""

# -----------------------------------------------------------------------------
# IMPORTS
# -----------------------------------------------------------------------------

from pathlib import Path

import argparse
import os
import time

from astropy.units import Quantity

import numpy as np

from hsr4hci.config import load_config
from hsr4hci.contrast_curves import get_injection_and_reference_positions
from hsr4hci.coordinates import cartesian2polar
from hsr4hci.data import load_dataset
from hsr4hci.forward_modeling import add_fake_planet
from hsr4hci.fits import save_fits
from hsr4hci.pca import get_pca_signal_estimates
from hsr4hci.units import set_units_for_instrument


# -----------------------------------------------------------------------------
# MAIN CODE
# -----------------------------------------------------------------------------

if __name__ == '__main__':

    # -------------------------------------------------------------------------
    # Preliminaries
    # -------------------------------------------------------------------------

    script_start = time.time()
    print('\nINJECT FAKE PLANET AND RUN PCA\n', flush=True)

    # -------------------------------------------------------------------------
    # Set up parser to get command line arguments
    # -------------------------------------------------------------------------

    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--experiment-dir',
        type=str,
        required=True,
        metavar='PATH',
        help='Path to experiment directory.',
    )
    args = parser.parse_args()

    # -------------------------------------------------------------------------
    # Load experiment configuration and data
    # -------------------------------------------------------------------------

    # Get experiment directory
    experiment_dir = Path(os.path.expanduser(args.experiment_dir))
    if not experiment_dir.exists():
        raise NotADirectoryError(f'{experiment_dir} does not exist!')

    # Get path to results directory
    results_dir = experiment_dir / 'results'
    results_dir.mkdir(exist_ok=True)

    # Load experiment config from JSON
    print('Loading experiment configuration...', end=' ', flush=True)
    config = load_config(experiment_dir / 'config.json')
    print('Done!', flush=True)

    # Load frames, parallactic angles, etc. from HDF file
    # By default, the stack is already loaded *without* the planet
    print('Loading data set...', end=' ', flush=True)
    stack, parang, psf_template, observing_conditions, metadata = load_dataset(
        **config['dataset']
    )
    print('Done!\n', flush=True)

    # -------------------------------------------------------------------------
    # Define various useful shortcuts; activate unit conversions
    # -------------------------------------------------------------------------

    # Quantities related to the size of the data set
    n_frames, x_size, y_size = stack.shape
    frame_size = (x_size, y_size)

    # Metadata of the data set
    pixscale = float(metadata['PIXSCALE'])
    lambda_over_d = float(metadata['LAMBDA_OVER_D'])

    # Activate the unit conversions for this instrument
    set_units_for_instrument(
        pixscale=Quantity(pixscale, 'arcsec / pixel'),
        lambda_over_d=Quantity(lambda_over_d, 'arcsec'),
        verbose=False,
    )

    # -------------------------------------------------------------------------
    # Inject a fake planet into the stack
    # -------------------------------------------------------------------------

    # Get injection parameters
    contrast = float(config['injection']['contrast'])
    separation = float(config['injection']['separation'])
    azimuthal_position = config['injection']['azimuthal_position']

    # Compute (Cartesian) position at which to inject the fake planet and
    # convert to a polar position (for add_fake_planet())
    print('Computing injection position...', end=' ', flush=True)
    injection_position_cartesian, _ = get_injection_and_reference_positions(
        separation=Quantity(separation, 'lambda_over_d'),
        azimuthal_position=azimuthal_position,
        aperture_radius=Quantity(0.5, 'lambda_over_d'),
        frame_size=frame_size,
    )
    injection_position_polar = cartesian2polar(
        position=injection_position_cartesian,
        frame_size=frame_size,
    )
    rho = injection_position_polar[0].to('pixel').value
    phi = injection_position_polar[1].to('degree').value + 90
    print(f'Done! (rho = {rho:.2f} pix, phi = {phi:.2f} deg)', flush=True)

    # Add fake planet with given parameters to the stack
    if contrast is None or separation is None or azimuthal_position is None:
        print('Skipping injection of a fake planet!', flush=True)
    else:
        print('Injecting fake planet...', end=' ', flush=True)
        stack = np.asarray(
            add_fake_planet(
                stack=stack,
                parang=parang,
                psf_template=psf_template,
                polar_position=injection_position_polar,
                magnitude=contrast,
                extra_scaling=1,
                dit_stack=float(metadata['DIT_STACK']),
                dit_psf_template=float(metadata['DIT_PSF_TEMPLATE']),
                return_planet_positions=False,
                interpolation='bilinear',
            )
        )
        print('Done!', flush=True)

    # -------------------------------------------------------------------------
    # Run PCA to get signal estimate and save as a FITS files
    # -------------------------------------------------------------------------

    # Run PCA (for a fixed number of principal components)
    print('Running PCA...', end=' ', flush=True)
    signal_estimate = np.asarray(
        get_pca_signal_estimates(
            stack=stack,
            parang=parang,
            pca_numbers=[int(config['n_components'])],
            roi_mask=None,
            return_components=False,
            n_processes=1,
            verbose=False,
        )
    )
    signal_estimate = signal_estimate.squeeze()
    print('Done!', flush=True)

    # Save signal estimate to FITS
    print('Saving signal estimate to FITS...', end=' ', flush=True)
    file_path = results_dir / 'signal_estimate.fits'
    save_fits(array=signal_estimate, file_path=file_path)
    print('Done!', flush=True)

    # -------------------------------------------------------------------------
    # Postliminaries
    # -------------------------------------------------------------------------

    print(f'\nThis took {time.time() - script_start:.1f} seconds!\n')