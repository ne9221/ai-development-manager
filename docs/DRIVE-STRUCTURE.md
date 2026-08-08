# Google Drive Structure (spec only - not created in v0.1)

This environment has no Google Drive API access, so nothing was created on
Drive this round. This document is the exact structure someone (or a
future collector setup step) should create by hand or via the Drive API.

```
AI Development Manager/
├─ AI-DEVELOPMENT-RULES.md      (mirror of the GitHub repo's copy, or a
│                                 pointer to it - decide in Phase 2)
├─ PROJECTS.md                  (empty placeholder for now)
├─ CHANGELOG.md                 (Drive-side changelog, separate from the
│                                 repo's code changelog)
├─ AI-RESOURCE-STATUS/
│  └─ status.json               (empty/placeholder; real content will be
│                                 shaped like schema/status.example.json
│                                 in the GitHub repo once collectors exist)
├─ TASKS/                       (empty)
├─ HANDOFFS/                    (empty)
└─ TASK-HISTORY/                (empty)
```

No Google Sheet is created in v0.1 per the round's explicit instruction.

## Why this isn't done automatically yet

Creating/writing these requires either interactive Google sign-in in a
browser or a Drive API service-account credential - neither is set up in
this environment, and setting one up is out of scope for this round. Until
then, Drive is not a working SSOT - it is only specified here so that
whoever does have Drive access can create it exactly as designed, and so
the local machine is not mistaken for the real SSOT in the meantime.
