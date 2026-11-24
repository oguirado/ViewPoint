<!-- SPDX-License-Identifier: AGPL-3.0-only -->

# Slot conformance licence fence

ViewPoint ships its original AGPL-3.0-only host code and host-authored fixtures. The user obtains and installs the external tool and any external adapter. ViewPoint binds the reviewed installation by digest and runs it out of process under the admitted operation policy. A licence decision alone grants neither runtime admission nor conformance.

## External installation

- Bind the review to the upstream source URL, release version, upstream licence identifier and evidence, relevant component licences and notices, implementation digest, adapter provenance, and intended use. Record the reviewer decision and its scope against the pinned release and installation.
- Permit enrollment when the reviewed terms cover that external use and the installation, adapter and prerequisites match their admitted identities. Treat a missing or contradictory record as a hold on the affected use. Recheck the record when the release, pinned bytes, components or intended use changes.
- Apply the research-only disposition for the distinct raea renderer and its FSL-1.1-ALv2 materials. A user-installed path qualifies only for the use explicitly admitted under that disposition. The MIT identifier for morluto/rea cannot be used to review a raea installation.

## Release artifacts

- Inventory the actual staged release, including nested archives, generated resources, bundled dependencies, and packaged links. Check each included item against the approved inputs, component licence and recorded provenance. Unresolved provenance or unresolved terms block publication of the affected artifact.
- Reject a vendored slot binary, code, skill, asset, copied implementation, or package link that incorporates a slot payload. Apply this rule even when the upstream licence would permit redistribution. Reject any component whose terms are incompatible with the intended distribution and any inclusion of research-only raea material. Reject relabelling of upstream material as AGPL-3.0-only.
- Permit discovery metadata and authored synthetic fixtures whose provenance and distribution have been reviewed. Registration and packaging may refer to a user-selected installation without including its contents.
- Retain the decision, affected artifact, pinned identity, reason and supporting provenance in the kit report. Record external execution, internal research and host distribution as separate dispositions.

## Verification gate and negative controls

- Nominal release inventory contains the reviewed host code and fixtures. A metadata-only registration points to a separately installed tool whose reviewed terms cover the admitted external use. External use and host publication each require their own passing decision.
- Add a synthetic inventory row marked as a bundled external binary with an MIT declaration. The kit rejects publication for vendoring despite the permissive licence declaration.
- Replace the provenance of a host fixture with research-only raea provenance and declare MIT. The kit rejects publication for restricted provenance and contradictory licence evidence.
- Remove the upstream licence evidence from a synthetic registration. The kit holds enrollment before dispatch and records the missing evidence.

Sources in the supplied package include REQUIREMENTS.md, SLOTHOST_ARCH.json#/licence_fence, SLOTHOST_ARCH.json#/principles and rulings U-RUL-0050 through U-RUL-0053. This fence scopes its decisions to the reviewed release, artifacts and intended uses.
