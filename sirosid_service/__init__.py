"""sirosid_service - the control plane behind the self-service instance manager.

Built on sirosid_core (which knows how to deploy, stop, start and destroy an
instance) and adds what a multi-user service needs: users and invites, quotas,
ownership, saved configs, a TTL reaper and an orphan sweeper. It has no HTTP, MCP
or passkey layer yet - those authenticate a caller and hand ControlPlane a
Principal; everything that decides what the caller may do lives here, so every
front end (web, MCP, CLI) gets the same rules.
"""
