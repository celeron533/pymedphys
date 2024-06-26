===================================
MacOS Setup
===================================

Overview
========

* Install Python 3.12.3
* Install `pipx` with ``pip install pipx``
* Install ``poetry`` with ``pipx install poetry``
* Install freetds

  * Can be done with `Homebrew`_; ``brew install freetds``
* Install ``cython``

  * eg. ``pip install cython``
* Clone the PyMedPhys git repo
* Run ``poetry install -E all`` within the root of the repo
* Run ``poetry run pre-commit install``
* Install pandoc

  * Can be done with `Homebrew`_; ``brew install pandoc``

You're good to go.

.. _`Homebrew`: https://brew.sh/


Opinionated Recommendations
===========================

* Install Python with `pyenv`_
* Install `VSCode`_ as your code editor
* Install `Jupyter Lab`_ to work with Notebooks


.. _`pyenv`: https://github.com/pyenv/pyenv-installer#install
.. _`VSCode`: https://code.visualstudio.com/Download
.. _`Jupyter Lab`: https://jupyterlab.readthedocs.io/en/stable/getting_started/installation.html#pip
