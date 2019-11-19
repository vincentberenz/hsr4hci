"""
Create toy data sets with fake planets.
"""

# -----------------------------------------------------------------------------
# IMPORTS
# -----------------------------------------------------------------------------

from pathlib import Path

import os

import h5py
import numpy as np

from hsr4hci.utils.config import get_data_dir
from hsr4hci.utils.data import load_data
from hsr4hci.utils.fits import save_fits
from hsr4hci.utils.forward_modeling import crop_psf_template, get_signal_stack


# -----------------------------------------------------------------------------
# MAIN CODE
# -----------------------------------------------------------------------------

if __name__ == '__main__':

    print('\nMAKE TOY DATASET\n')

    # -------------------------------------------------------------------------
    # Load data on which the toy data set will be based
    # -------------------------------------------------------------------------

    print('Loading Beta Pictoris data...', end=' ', flush=True)

    # Define the data set that we want to load
    data_dir = get_data_dir()
    dataset_config = \
        {"file_path": f"{data_dir}/Beta_Pictoris/Lp/2013-02-01/processed/"
                      f"stacked_50.hdf",
         "stack_key": "/stack",
         "parang_key": "/parang",
         "psf_template_key": "/psf_template",
         "pixscale": 0.0271,
         "lambda_over_d": 0.1,
         "frame_size": (81, 81),
         "subsample": 1,
         "presubtract": None}

    # Load frames, parallactic angles and PSF template. The base_stack will
    # serve as the background "noise" into which we add some generated fake
    # planets.
    base_stack, parang, psf_template = load_data(dataset_config=dataset_config)

    # Crop the PSF template to desired size
    psf_cropped = \
        crop_psf_template(psf_template=psf_template,
                          psf_radius=5,
                          rescale_psf=True,
                          pixscale=dataset_config['pixscale'],
                          lambda_over_d=dataset_config['lambda_over_d'])

    print('Done!', flush=True)

    # -------------------------------------------------------------------------
    # Create fake planets and add them on top of the "noise"
    # -------------------------------------------------------------------------

    print('Creating fake planets...', end=' ', flush=True)

    # Define dummy stack to keep adding up the pure signal
    signal = np.zeros(base_stack.shape)

    # Loop over positions and amplitudes and compute forward models
    for position, amplitude in [((42, 23), 10),
                                ((32, 41), 20)]:

        # Generate the forward model (i.e., the fake planet signal)
        tmp_signal, planet_positions = \
            get_signal_stack(position=position,
                             frame_size=dataset_config['frame_size'],
                             parang=parang,
                             psf_cropped=psf_cropped)

        # Scale to desired amplitude
        tmp_signal /= np.max(tmp_signal)
        tmp_signal *= amplitude

        # Add planet to the signal stack
        signal += tmp_signal

    print('Done!', flush=True)
    print('Adding signal to noise...', end=' ', flush=True)

    # Add simulated signals on top of the "noise" (i.e., the base data stack)
    stack = base_stack + signal

    print('Done!', flush=True)

    # -------------------------------------------------------------------------
    # Save results to an HDF file (and also as a FITS file)
    # -------------------------------------------------------------------------

    print('Saving toy dataset...', end=' ', flush=True)

    # Prepare output directory
    toy_datasets_dir = os.path.join(data_dir, 'toy_datasets', 'Beta_Pictoris')
    Path(toy_datasets_dir).mkdir(exist_ok=True, parents=True)

    # Save results as a HDF file
    file_path = os.path.join(data_dir, toy_datasets_dir, 'default.hdf')
    with h5py.File(file_path, 'w') as hdf_file:
        hdf_file.create_dataset(name='stack', data=stack)
        hdf_file.create_dataset(name='parang', data=parang)
        hdf_file.create_dataset(name='psf_template', data=psf_template)

    # Save results as a FITS file
    file_path = os.path.join(data_dir, toy_datasets_dir, 'default.fits')
    save_fits(stack, file_path=file_path)

    # Save pure signal as a FITS file (this can be a useful comparison for
    # the detection map computed by the half-sibling regression)
    file_path = os.path.join(data_dir, toy_datasets_dir, 'default_signal.fits')
    save_fits(signal, file_path=file_path)

    print('Done!\n', flush=True)
