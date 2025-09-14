# Hyperliquid

Integration tests connect to the Hyperliquid API over HTTPS and WebSockets.
To verify the SSL certificates presented by the servers, the tests rely on
[`certifi`](https://pypi.org/project/certifi/) for a trusted Certificate
Authority bundle. Ensure `certifi` is installed so these connections can be
established securely.

