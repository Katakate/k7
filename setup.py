from pathlib import Path

from setuptools import find_packages, setup

setup(
    name="k7-sdk",
    version="0.2.1",
    description="K7 sandbox management Python SDK (HTTP client for the k7 API)",
    long_description=Path(__file__).with_name("README.md").read_text(),
    long_description_content_type="text/markdown",
    url="https://github.com/Katakate/k7",
    license="Apache-2.0",
    packages=find_packages(
        where="src",
        include=["k7_sdk", "k7_sdk.*", "katakate", "katakate.*"],
    ),
    package_dir={"": "src"},
    include_package_data=True,
    install_requires=[
        "requests>=2.31.0",
    ],
    extras_require={
        "async": ["httpx>=0.27.0"],
        "sdk-async": ["httpx>=0.27.0"],  # back-compat extra name
    },
    python_requires=">=3.8",
)
