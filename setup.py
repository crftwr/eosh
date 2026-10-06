"""Package discovery that pyproject.toml can't express.

Everything else lives in pyproject.toml.  This file exists for one thing:
``addons/<name>/`` is installed as ``eosh_addons.<name>``.  The repository
keeps add-ons one directory below the top so each reads as its own unit;
the ``eosh_addons`` import namespace keeps their names from colliding with
anything else on ``sys.path``.  setuptools' ``packages.find`` reports names
relative to the directory it searched (``awsut``, not
``eosh_addons.awsut``), so the list is built here.

``eosh_addons`` is a namespace package — ``addons/`` has no
``__init__.py`` — so an add-on that later ships as its own distribution can
keep its import name.
"""

from setuptools import find_packages, setup

setup(
    package_dir={"": "src", "eosh_addons": "addons"},
    packages=find_packages("src") + [
        "eosh_addons." + name for name in find_packages("addons")
    ],
)
