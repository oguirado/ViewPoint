# Operator-fetched VM packages

AUSENTE — vendored packages and Ubuntu digest are not present before the operator fetch.
Run scripts/vm/vendor_fetch.sh explicitly on the operator host. It refuses existing
vendor artifacts. Fetching is the only network step; builds use network none.
The fetch resolves absent dependencies against the pulled arm64 Ubuntu base and
writes .deb files, SHA256SUMS and DIGEST. No package is downloaded by the image build.
