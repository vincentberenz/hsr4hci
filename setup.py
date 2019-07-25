from setuptools import setup

setup(name='hsr4hci',
      version='epsilon',
      description='hsr4hci: Half-Sibling Regression for High-Contrast Imaging',
      url='https://github.com/timothygebhard/hsr4hci',
      install_requires=['astropy',
                        'h5py',
                        'joblib',
                        'matplotlib',
                        'numpy',
                        'scikit-learn',
                        'tqdm'],
      packages=['hsr4hci'],
      zip_safe=False)