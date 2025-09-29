"""Setup script for fcrawl CLI tool"""

from setuptools import setup, find_packages

with open('requirements.txt') as f:
    requirements = f.read().splitlines()

setup(
    name='fcrawl',
    version='0.1.0',
    description='A powerful CLI tool for Firecrawl - web scraping from your terminal',
    author='Your Name',
    python_requires='>=3.8',
    packages=find_packages(),
    install_requires=requirements,
    entry_points={
        'console_scripts': [
            'fcrawl=fcrawl:cli',
        ],
    },
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
    ],
)