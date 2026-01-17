#!/usr/bin/env python3
"""Setup configuration for Harmony - Universal Playlist Manager."""

from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="harmony-playlist-manager",
    version="0.1.0",
    author="Ara Saba",
    description="Universal playlist manager for music services (Plex, Navidrome, etc.)",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/arsaboo/harmony",
    license='MIT',
    platforms='ALL',
    packages=find_packages(include=['harmony', 'harmony.*']),
    python_requires=">=3.9",
    install_requires=[
        "typer[all]>=0.9.0",
        "plexapi>=4.13.0",
        "pydantic>=2.0.0",
        "pydantic-settings>=2.0.0",
        "rich>=13.0.0",
        "requests>=2.28.0",
        "beautifulsoup4>=4.11.0",
        "spotipy>=2.22.0",
        "python-dateutil>=2.8.2",
        "numpy>=1.24.0",
        "enlighten>=1.10.0",
    ],
    extras_require={
        "beets": ["beets>=1.6.0"],
        "ai": [
            "agno>=1.2.16",
            "instructor>=1.0.0",
            "openai>=1.0.0",
            "tavily-python>=0.1.0",
            "exa_py>=1.0.0",
        ],
        "dev": [
            "pytest>=7.0.0",
            "pytest-cov>=4.0.0",
            "black>=23.0.0",
            "flake8>=5.0.0",
            "mypy>=1.0.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "harmony=harmony.cli:main",
        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
)
