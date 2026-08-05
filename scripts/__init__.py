"""Hivemind operator/rehearsal scripts.

Making this a package ensures ``import scripts.<module>`` resolves to THIS
repository's scripts directory even when an unrelated site-packages ``scripts``
package (e.g. agentkit) is installed — a regular package wins over a bare
namespace directory on sys.path.
"""
