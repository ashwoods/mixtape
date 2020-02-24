#!/usr/bin/env python

"""
mixtape: awesome mix vol 1 -> python asyncio gstreamer application mini-framework 

"""
from setuptools import setup, find_packages
from os.path import dirname, abspath
import os.path

PARENT_DIR = dirname(abspath(__file__))

def get_version():  # move versioning into __init__.py
    return "0.4.1"

test_deps = [
    'pytest',
    'colorlog',
    'pytest-cov',
    'pytest-flake8',
    'flake8-bugbear',
    'pytest-black',
    'pytest-asyncio',
    'pytest-benchmark',
    'pytest-profiling',
    'pytest-leaks',
    'pytest-bandit',
    'memory_profiler',
    'pytest-xdist',
    'teamcity-messages',
    'pdbpp',
]
extras = {
    'test': test_deps,
}

setup(
    name="mixtape",
    version=get_version(),
    author="Ashley Camba Garrido",
    author_email="ashwoods@gmail.com",
    url="https://github.com/ashwoods/mixtape",
    description="Gstreamer python application mini-framework",
    long_description=__doc__,
    packages=find_packages(exclude=("tests", "tests.*")),
    zip_safe=False,
    license="MIT",
    tests_require=test_deps,
    extras_require=extras,
    install_requires=[
        'attrs',
        'pkgconfig',
        'pampy',
        'beppu'
        ],
    classifiers=[
        "Development Status :: 2 - Pre-Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
)
