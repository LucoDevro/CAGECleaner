#!/usr/bin/env python

from setuptools import setup

with open("README.md") as readme:
    long_description = readme.read()

setup(name = "cagecleaner",
      version = "1.1.0",
      author="Lucas De Vrieze",
      author_email="lucas.devrieze@kuleuven.be",
      license = "MIT",
      description = "Genomic redundancy removal tool for cblaster hit sets",
      long_description = long_description,
      long_description_content_type = "text/markdown",
      python_requires = ">=3.10",
      classifiers = [
          "Programming Language :: Python :: 3",
          "License :: OSI Approved :: MIT License",
          "Operating System :: OS Independent",
          "Intended Audience :: Science/Research",
          "Topic :: Scientific/Engineering :: Bio-Informatics",
      ],
      entry_points = {"console_scripts": ['cagecleaner = cagecleaner.cagecleaner:main']},
      scripts = ['src/cagecleaner/dereplicate_assemblies.sh',
                 'src/cagecleaner/download_assemblies.sh',
                 'src/cagecleaner/get_accessions.sh'],
      install_requires=[
          "scipy",
          "more-itertools",
          "Biopython",
          "cblaster",
          "clinker",
          "pandas",
      ],
      )
