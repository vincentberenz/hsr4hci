"""
Evaluate (compute SNR) and plot signal estimate.
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
import pandas as pd

from hsr4hci.config import load_config
from hsr4hci.coordinates import polar2cartesian
from hsr4hci.data import load_psf_template, load_metadata, load_planets
from hsr4hci.evaluation import compute_optimized_snr
from hsr4hci.fits import read_fits
from hsr4hci.plotting import plot_frame
from hsr4hci.psf import get_psf_fwhm
from hsr4hci.units import set_units_for_instrument


# -----------------------------------------------------------------------------
# MAIN CODE
# -----------------------------------------------------------------------------

if __name__ == '__main__':

    # -------------------------------------------------------------------------
    # Preliminaries
    # -------------------------------------------------------------------------

    script_start = time.time()
    print('\nEVALUATE AND PLOT SIGNAL ESTIMATE\n', flush=True)

    # -------------------------------------------------------------------------
    # Set up parser to get command line arguments
    # -------------------------------------------------------------------------

    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--experiment-dir',
        type=str,
        required=True,
        metavar='PATH',
        help='(Absolute) path to experiment directory.',
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

    # Load the PSF template and estimate its FWHM
    print('Loading PSF template...', end=' ', flush=True)
    psf_template = load_psf_template(**config['dataset']).squeeze()
    psf_fwhm = round(get_psf_fwhm(psf_template), 2)
    print(f'Done! (psf_radius = {psf_fwhm})', flush=True)

    # Load the metadata and set up the unit conversions
    print('Loading metadata and setting up units...', end=' ', flush=True)
    metadata = load_metadata(**config['dataset'])
    pixscale = Quantity(metadata['PIXSCALE'], 'arcsec / pix')
    lambda_over_d = Quantity(metadata['LAMBDA_OVER_D'], 'arcsec')
    set_units_for_instrument(
        pixscale=pixscale,
        lambda_over_d=lambda_over_d,
        verbose=False,
    )
    print('Done!', flush=True)

    # -------------------------------------------------------------------------
    # Load signal estimate from FITS and compute SNRs
    # -------------------------------------------------------------------------

    # Load signal estimate
    print('Loading signal estimate...', end=' ', flush=True)
    file_path = results_dir / 'signal_estimate.fits'
    signal_estimate = np.asarray(read_fits(file_path=file_path))
    frame_size = signal_estimate.shape
    print('Done!', flush=True)

    # Load information about the planets in the dataset
    planets = load_planets(**config['dataset'])

    # Loop over all planets in the data set
    print('Computing (optimized) SNR...', end=' ', flush=True)
    results = dict()
    for name, parameters in planets.items():

        # Compute the expected planet position in Cartesian coordinates
        planet_position = polar2cartesian(
            separation=Quantity(parameters['separation'], 'arcsec'),
            angle=Quantity(parameters['position_angle'], 'degree'),
            frame_size=frame_size,
        )

        # Compute the figures of merit. The try/except is required for planets
        # that are so close to the star that we cannot drop any neighboring
        # apertures when computing the reference values for the noise.
        try:
            ignore_neighbors = 1
            results_dict = compute_optimized_snr(
                frame=signal_estimate,
                position=planet_position,
                aperture_radius=Quantity(psf_fwhm / 2, 'pixel'),
                ignore_neighbors=ignore_neighbors,
            )
        except ValueError:
            ignore_neighbors = 0
            results_dict = compute_optimized_snr(
                frame=signal_estimate,
                position=planet_position,
                aperture_radius=Quantity(psf_fwhm / 2, 'pixel'),
                ignore_neighbors=ignore_neighbors,
            )

        # Store relevant subset of the results dict
        results[name] = dict(
            signal=results_dict['signal'],
            noise=results_dict['noise'],
            snr=results_dict['snr'],
            fpf=results_dict['fpf'],
            old_position=tuple(
                map(lambda _: round(_, 2), results_dict['old_position'])
            ),
            new_position=results_dict['new_position'],
            success=results_dict['success'],
            ignore_neighbors=ignore_neighbors,
        )

    print('Done!\n', flush=True)

    # Convert results to a data frame and print results
    results_df = pd.DataFrame(results)
    print('RESULTS:')
    print(results_df, '\n')

    # Save data frame to TSV file in results directory
    print('Saving figures of merit to TSV...', end=' ', flush=True)
    file_path = results_dir / 'figures_of_merit.tsv'
    results_df.to_csv(file_path, sep='\t')
    print('Done!', flush=True)

    # -------------------------------------------------------------------------
    # Create plot of signal estimate
    # -------------------------------------------------------------------------

    # Ensure that there exists a plots directory in the experiment directory
    plots_dir = experiment_dir / 'plots'
    plots_dir.mkdir(exist_ok=True)

    # Create plot (including SNR etc.)
    print('Creating plot of signal estimate...', end=' ', flush=True)
    file_path = plots_dir / 'signal_estimate.pdf'
    plot_frame(
        frame=signal_estimate,
        file_path=file_path,
        aperture_radius=(psf_fwhm / 2),
        expand_radius=1,
        positions=list(results_df.loc['new_position'].values),
        snrs=list(results_df.loc['snr'].values),
    )
    print('Done!', flush=True)

    # -------------------------------------------------------------------------
    # Postliminaries
    # -------------------------------------------------------------------------

    print(f'\nThis took {time.time() - script_start:.1f} seconds!\n')