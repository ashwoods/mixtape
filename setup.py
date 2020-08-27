#!/usr/bin/env python
# type: ignore
"""
mixtape: awesome mix vol 1 -> python asyncio gstreamer application mini-framework
"""
from setuptools import setup, find_packages


def parse_requirements(filename):
    """ load requirements from a pip requirements file """
    lineiter = (line.strip() for line in open(filename))
    return [line for line in lineiter if line and not line.startswith("#")]


TEST_DEPS = parse_requirements("req-test.txt")
INSTALL_DEPS = parse_requirements("req-install.txt")
EXTRAS = {
    "test": TEST_DEPS,
}


setup(
    name="mixtape",
    version="0.6.0.dev",
    author="Ashley Camba Garrido",
    author_email="ashwoods@gmail.com",
    url="https://github.com/ashwoods/mixtape",
    description="Gstreamer python application mini-framework",
    long_description=__doc__,
    packages=find_packages(exclude=("tests", "tests.*")),
    # PEP 561
    package_data={"mixtape": ["py.typed"]},
    zip_safe=False,
    license="MIT",
    tests_require=TEST_DEPS,
    extras_require=EXTRAS,
    install_requires=INSTALL_DEPS,
    entry_points={"console_scripts": ["mixtape = mixtape.cli:play"]},
    classifiers=[
        "Development Status :: 2 - Pre-Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
)
