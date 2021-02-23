"""
Setup script to install hsr4hci as a Python package.
"""

# -----------------------------------------------------------------------------
# IMPORTS
# -----------------------------------------------------------------------------

from setuptools import setup


# -----------------------------------------------------------------------------
# RUN setup() FUNCTION
# -----------------------------------------------------------------------------

setup(name='hsr4hci',
      version='epsilon',
      description='hsr4hci: Half-Sibling Regression for High-Contrast Imaging',
      url='https://github.com/timothygebhard/hsr4hci',
      install_requires=[
          'astropy',
          'bottleneck',
          'contexttimer',
          'h5py',
          'joblib',
          'jupyter',
          'matplotlib>=3.3.2',
          'numpy',
          'pandas',
          'peakutils',
          'photutils',
          'pynpoint==0.9.0',
          'pytest',
          'requests',
          'scikit-image',
          'scikit-learn',
          'scipy',
          'seaborn',
          'tqdm',
      ],
      packages=['hsr4hci'],
      zip_safe=False,
      entry_points={
        'console_scripts': [
            'compute_snr = scripts.compute_snr:main',
        ]},
      )
