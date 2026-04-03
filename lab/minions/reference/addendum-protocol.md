# Addendum Protocol

When new context is discovered after an issue has been created (additional affected files, edge cases, related issues, cross-references), it must be appended as a **comment** on the relevant GitHub issue:

```bash
gh issue comment <number> --body "## Addendum (discovered during implementation)

- Additional affected file: \`path/to/file.py\`
- Related edge case: [description]
- Cross-reference: see also #NN"
```

This ensures the issue remains the single source of truth even as understanding evolves. The issue creator establishes the protocol; implementers execute it during fixes.
