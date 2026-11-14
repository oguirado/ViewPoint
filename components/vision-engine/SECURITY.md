# Security policy

Report vulnerabilities privately through GitHub private vulnerability reporting
on this repository ("Security" tab, "Report a vulnerability"). Do not open a
public issue for a security problem.

Supported version: the latest release (1.1.x).

Scope: vision-engine analyses frames locally (in RAM) and has no network
surface of its own. Reports about parsing of untrusted frame/JSON input, file
writes outside the requested output directory, or subprocess use (optional
tesseract and cargo) are in scope.
