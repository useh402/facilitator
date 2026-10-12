# Security

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository (Security tab, "Report a vulnerability"), or write to contact@h402.io. Please do not open a public issue for anything that could be exploited.

You will get an acknowledgement within three working days. Fixes ship as soon as they are ready; the report is credited if you want it to be.

## Scope

Everything in this repository, and the deployed services it describes. The TaskEscrow contract on Robinhood Chain mainnet is at `0x530C2beDB048a658cB9fFD074eB7A75D68bfBd90`; a bug in it is in scope.

## What is never in this repository

No private key, API token, database password or production data is committed here. Configuration comes from environment variables; the `*.example` files list every variable with placeholders only. If you find something that looks like a real secret anywhere in the history, report it the same way.
