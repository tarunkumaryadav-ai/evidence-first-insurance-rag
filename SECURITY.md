# Security policy

## Supported version

Security fixes are applied to the latest revision on the `main` branch.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability or exposed secret.
Use the repository's **Security** tab and select **Report a vulnerability**. Include
a concise description, affected component, reproduction steps, and potential
impact. Do not include live credentials in screenshots or reproduction data.

## Deployment guidance

The default configuration is intended for localhost. Before exposing the API:

- set a long random `RAG_API_KEY` and terminate TLS at a trusted reverse proxy;
- set a separate long random `RAG_DOCUMENT_SIGNING_KEY`; keep PDF evidence links short-lived;
- treat signed PDF links as temporary bearer credentials and add identity-based,
  per-document authorization before serving confidential files to multiple users;
- scan uploads for malware in deployments that accept files from untrusted users;
- keep uploaded PDFs in private storage outside static assets and encrypt storage/backups where required;
- restrict Ollama and Qdrant to private interfaces;
- use external persistent Qdrant rather than the embedded single-process store;
- limit ingress, request size, crawler egress, and filesystem permissions;
- keep `.env`, indexes, source snapshots, logs, and chat data out of source control;
- monitor dependency advisories and rebuild from the reviewed lock file.

Website content is untrusted input. The extraction and prompting layers must never
treat retrieved page text as system instructions or authorization to call tools.
