# Versioning policy

Both projects use MAJOR.MINOR.PATCH (X.Y.Z):

- PATCH (0.1.x): typo corrections, bug fixes and maintenance without new features.
- MINOR (0.x.0): new features or changes that are not major; reset PATCH to zero.
- MAJOR (x.0.0): major changes or the transition from beta/development to a final release; reset MINOR and PATCH to zero.

The engine and GUI have independent version numbers. The GUI pins the bundled engine version. Each release updates its changelog, documentation and download artifacts together. A leading zero denotes the current development stage, not a promise of API stability.
