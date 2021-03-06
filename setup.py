#!/usr/bin/env python
from setuptools import setup, find_packages
import sys
import os
import glob

setup(name = "scilifelab",
      version = "0.2",
      author = "Science for Life Laboratory",
      author_email = "genomics_support@scilifelab.se",
      description = "Useful scripts for use at SciLifeLab",
      license = "MIT",
      scripts = glob.glob('scripts/*.py') + glob.glob('scripts/bcbb_helpers/*.py') + ['scripts/pm'],
      install_requires = [
        "bcbio-nextgen >= 0.2",
        "drmaa >= 0.5",
        "sphinx >= 1.1.3",
        "couchdb >= 0.8",
        "reportlab >= 2.5",
        "cement >= 2.0.2",
        "mock",
        "PIL"
        ],
      test_suite = 'nose.collector',
      packages=find_packages(exclude=['tests']),
      package_data = {'scilifelab':[
            'data/grf/*',
            ]}
      )

os.system("git rev-parse --short --verify HEAD > ~/.scilifelab_version")
