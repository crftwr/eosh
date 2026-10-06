"""``awsut sagemaker`` — jobs, hub content, Studio, and HyperPod clusters.

A subpackage of the add-on rather than a module beside :mod:`..cli`, which
is long enough already.  It is attached from inside ``cli.register()``
because ``CommandRegistry._make_root`` *overwrites* an existing root: a
second module cannot re-open the ``awsut`` root without discarding the tree
``cli`` already built.
"""

from __future__ import annotations


def register_sagemaker(awsut_root) -> None:
    """Attach the ``sagemaker`` group to the ``awsut`` command tree."""
    # Imported here rather than at module scope: these modules do
    # ``from .. import cli``, and ``cli.register()`` is what calls this.
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
