"""meshwars-bot: relays MeshWars public announcements onto a mesh channel.

Standalone, deliberately NOT part of MeshWars. MeshWars has no transmit
capability and must never gain one — this bot only ever reads
`GET {base_url}/api/v1/announcements` and (in a later task) writes to a
radio transport.
"""

__version__ = "0.1.0"
