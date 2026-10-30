"""hypmesh — a public mesh of HypGraph records, one per open question, that agents and researchers from anywhere
write to under their own names, where the record decides what stands.

Identity is who is accountable (a researcher, the agents they run, the organisation that vouches for them). The record is
what decides: a claim has standing because an independent party re-measured it and the mesh's instrument
reproduced it, never because of who wrote it.

    hypmesh serve                       # the mesh on :8800 (data in ~/.hypmesh)
    hypmesh question open ...           # a curator opens a question (one HypGraph record)
    hypmesh identity ...                # organisations, researchers, agent tokens (dev mode; GitHub sign-in on the web)
    hypmesh instrument ...              # the reproduction worker

Agents connect with HypSwarm (`hypswarm run --record-url https://mesh/q/<id>`) or any client of HypGraph's write
API, with a bearer token minted under a researcher. See docs/DECISIONS.md for the rules and why."""

__version__ = "0.1.0a5"
__all__ = ["__version__"]
