from setuptools import setup


# semver with automatic minor bumps keyed to unix time
__version__ = '1.3.1534953287'


setup(
    name='py9backup',
    version=__version__,
    packages=[],
    scripts=['py9backup/backup'],
    install_requires=['click'],
)
