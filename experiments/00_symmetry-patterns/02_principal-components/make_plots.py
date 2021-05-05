"""
Run PCA and plot the principal components / eigenimages.
"""

# -----------------------------------------------------------------------------
# IMPORTS
# -----------------------------------------------------------------------------

from pathlib import Path

import time

from mpl_toolkits.axes_grid1.anchored_artists import AnchoredSizeBar

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np

from hsr4hci.coordinates import get_center
from hsr4hci.data import load_dataset
from hsr4hci.plotting import disable_ticks
from hsr4hci.pca import get_pca_signal_estimates


# -----------------------------------------------------------------------------
# MAIN CODE
# -----------------------------------------------------------------------------

if __name__ == '__main__':

    # -------------------------------------------------------------------------
    # Preliminaries
    # -------------------------------------------------------------------------

    script_start = time.time()
    print('\nRUN PCA AND PLOT PRINCIPAL COMPONENTS\n', flush=True)

    # -------------------------------------------------------------------------
    # Run PCA on all data sets and plot the principal components / eigenimages
    # -------------------------------------------------------------------------

    # Ensure the plots directory exists
    plots_dir = Path('plots')
    plots_dir.mkdir(exist_ok=True)

    # Loop over different data sets
    for dataset in (
        'beta_pictoris__lp',
        'beta_pictoris__mp',
        'hr_8799__lp',
        'r_cra__lp',
    ):

        start_time = time.time()
        print(f'Running for {dataset}...', end=' ', flush=True)

        # Load data set (and crop to some reasonable size)
        stack, parang, psf_template, obs_con, metadata = load_dataset(
            name=dataset,
            frame_size=(51, 51),
            binning_factor=1,
        )
        n_frames, x_size, y_size = stack.shape
        center = get_center((x_size, y_size))

        _, components = get_pca_signal_estimates(
            stack=stack,
            parang=parang,
            pca_numbers=[24],
            return_components=True,
        )

        for n in range(24):

            plot_array = components[n]

            # Prepare grid for the pcolormesh()
            x_range = np.arange(x_size)
            y_range = np.arange(y_size)
            x, y = np.meshgrid(x_range, y_range)

            # Plot the result
            fig, ax = plt.subplots(figsize=(3, 3))
            limit = 1.1 * np.max(np.percentile(plot_array, 99.95))
            img = ax.pcolormesh(
                x,
                y,
                plot_array,
                shading='nearest',
                cmap='RdBu_r',
                rasterized=True,
                vmin=-limit,
                vmax=limit,
            )
            ax.plot(center[0], center[1], '+', color='black', ms=12)
            disable_ticks(ax)

            # Create the scale bar and add it to the frame
            scalebar = AnchoredSizeBar(
                transform=ax.transData,
                size=0.5 / float(metadata['PIXSCALE']),
                label='0.5"',
                loc=2,
                pad=1,
                color='black',
                frameon=False,
                size_vertical=0,
                fontproperties=fm.FontProperties(size=12),
            )
            ax.add_artist(scalebar)

            # Ensure that the results directory for this data set exists
            dataset_dir = plots_dir / dataset
            dataset_dir.mkdir(exist_ok=True)

            # Save the plot as a PDF
            fig.tight_layout()
            file_path = dataset_dir / f'{dataset}__n_pc={n}.pdf'
            plt.savefig(file_path, bbox_inches='tight', pad_inches=0.025)
            plt.close()

        print(f'Done! ({time.time() - start_time:.1f} seconds)', flush=True)

    # -------------------------------------------------------------------------
    # Postliminaries
    # -------------------------------------------------------------------------

    print(f'\nThis took {time.time() - script_start:.1f} seconds!\n')
