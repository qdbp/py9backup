from setuptools import setup


# semver with automatic minor bumps keyed to unix time
__version__ = '1.4.1604272276'


setup(
    name='py9backup',
    version=__version__,
    packages=[],
    scripts=['py9backup/backup'],
    install_requires=['click'],
)
