#!/usr/bin/env python
# type: ignore
"""
mixtape: awesome mix vol 1 -> python asyncio gstreamer application mini-framework
"""
from setuptools import setup, find_packages


TEST_DEPS = [
    "pytest==5.4.2",
    "pytest-asyncio==0.12.0",
    "pytest-benchmark==3.2.3",
    "pytest-black==0.3.9",
    "pytest-cov==2.9.0",
    "pytest-flake8==1.0.6",
    "pytest-forked==1.1.3",
    "pytest-leaks==0.3.1",
    "pytest-mock==3.1.0",
    "pytest-mypy==0.6.2",
    "pytest-mypy-plugins==1.3.0",
    "pytest-profiling==1.7.0",
    "colorlog==4.1.0",
    "flake8-bugbear==20.1.4",
    "PyGObject-stubs==0.0.2",
    "pdbpp",
]
EXTRAS = {
    "test": TEST_DEPS,
}

setup(
    name="mixtape",
    version="0.5.0.dev0",
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
    install_requires=["attrs", "pampy", "beppu"],
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
