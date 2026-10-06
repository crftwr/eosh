"""awsut — AWS utility commands for eosh, as a bundled add-on.

The command tree (``awsut whoami``, ``awsut sagemaker …``,
``awsut bedrock-agentcore …``) is built in :mod:`.cli`; the per-service
groups live in the ``sagemaker`` and ``agentcore`` subpackages.  Enable it
from ``~/.eosh/config.py`` with ``enable("awsut")`` (or ``enable("*")``).

Needs the ``awsut`` extra (boto3, pexpect): ``pip install 'eosh[awsut]'``.

Like every add-on under ``addons/``, it uses only eosh's public API —
``tests/addons/test_boundary.py`` enforces that.
"""

from .cli import register  # noqa: F401
