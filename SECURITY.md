# Security

## Reporting a vulnerability

Open a private security advisory on this repository
(**Security → Report a vulnerability**). Please do not open a public issue for
something exploitable.

Tell us what you can reach and how; a proof of concept against your own rig is
worth more than a description. We will confirm within a few days and tell you
what we intend to do and when.

## What this software assumes

A rig is a computer with physical access to hardware, and the service on it
flashes firmware and runs code that came from somewhere else. That shapes what
is and is not a vulnerability here.

**Trusted:** whoever holds an API key of the right role, and whatever a run's
own suite does on the boards it was granted. A rig runs what its owner tells
it to run.

**Not trusted, and these are bugs:** anything that lets a caller without a key
read or change what a key is for; a run reaching a board it was not granted;
a request escaping the artifact store by naming its way out; a secret reaching
a log, an evidence file, a dashboard, or a URL.

**Secrets on a rig** live in files the service reads, never in a command line
and never in a URL. Provider credentials are sealed to the rig's own key: a
portal relays one and cannot read it. The job log is scrubbed as it is
written, not afterwards, because a node streams it while the run is going.

**A portal** is the multi-tenant part, and it is the part to look hardest at:
accounts, sessions, enrolment tokens, and what one rig's owner can see of
another's. Routes are default-deny; a route that answers without a key is
named in a list on purpose.

## What is out of scope

Physical access to the rig. Denial of service by submitting runs, which is
what a queue is for. The simulator, which is a test fixture.
