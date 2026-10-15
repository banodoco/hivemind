"""Hivemind operator/rehearsal scripts.

The package is deliberately named ``hivemind_scripts`` rather than ``scripts``.
A pack root can end up on a shared PYTHONPATH next to other repositories (for
example Astrid worker processes, whose own checkout also has a top-level
``scripts`` package), and an unrelated site-packages ``scripts`` (e.g. agentkit)
may be installed as well. A unique name means ``import hivemind_scripts.<module>``
always resolves to THIS repository and can never shadow, or be shadowed by,
another repo's ``scripts``.
"""
