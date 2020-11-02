from setuptools import find_packages, setup

# semver with automatic minor bumps keyed to unix time
__version__ = "1.5.1604274141"


setup(
    name="py9backup",
    version=__version__,
    packages=find_packages(),
    entry_points={"console_scripts": ["backup=py9backup.backup:main"]},
    install_requires=["click"],
)
