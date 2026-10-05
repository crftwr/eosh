"""``awsut sagemaker`` — jobs, hub content, Studio, and HyperPod clusters.

A private subpackage rather than a second recipe module, for two reasons:

* ``recipes/_discover_all_recipes()`` globs ``recipes/*.py`` and calls
  ``register()`` on every match, so a sibling module would be picked up by
  ``enable("*")`` as a recipe in its own right.  Directories are not globbed.
* ``CommandRegistry._make_root`` *overwrites* an existing root, so a second
  module cannot re-open the ``awsut`` root without discarding the tree
  ``awsut.py`` already built.  The group is therefore registered from inside
  ``awsut.register()``, which owns that root.

The entry point is deliberately **not** named ``register`` — see the first
point above.
"""

from __future__ import annotations


def register_sagemaker(awsut_root) -> None:
    """Attach the ``sagemaker`` group to the ``awsut`` command tree."""
    # Imported here rather than at module scope: these modules do
    # ``from .. import awsut``, and ``awsut.register()`` is what calls this.
    from .hub import register_hub
    from .hyperpod import register_hyperpod
    from .jobs import register_jobs
    from .studio import register_studio

    sagemaker = awsut_root.command(
        "sagemaker",
        help="SageMaker jobs, hub content, Studio, and HyperPod clusters",
    )
    register_jobs(sagemaker)
    register_hub(sagemaker)
    register_studio(sagemaker)
    register_hyperpod(sagemaker)
