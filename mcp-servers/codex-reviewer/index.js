#!/usr/bin/env node

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { spawn } from "child_process";
import { promisify } from "util";

// Execute codex and return output
async function runCodex(prompt, options = {}) {
  const { cwd = process.cwd(), timeout = 300000 } = options;

  return new Promise((resolve, reject) => {
    const args = ["exec", prompt];

    const proc = spawn("codex", args, {
      cwd,
      shell: true,
      timeout,
      env: { ...process.env, FORCE_COLOR: "0" },
    });

    let stdout = "";
    let stderr = "";

    proc.stdout.on("data", (data) => {
      stdout += data.toString();
    });

    proc.stderr.on("data", (data) => {
      stderr += data.toString();
    });

    proc.on("close", (code) => {
      if (code === 0) {
        resolve({ success: true, output: stdout, stderr });
      } else {
        resolve({
          success: false,
          output: stdout,
          stderr,
          exitCode: code
        });
      }
    });

    proc.on("error", (err) => {
      reject(new Error(`Failed to run codex: ${err.message}`));
    });
  });
}

// Parse Codex output for structured issues
function parseReviewOutput(output) {
  const issues = {
    blocking: [],
    concerns: [],
    suggestions: [],
  };

  const lines = output.split("\n");
  let currentSection = null;

  for (const line of lines) {
    const lower = line.toLowerCase();

    if (lower.includes("blocking") || lower.includes("must fix")) {
      currentSection = "blocking";
    } else if (lower.includes("concern") || lower.includes("should")) {
      currentSection = "concerns";
    } else if (lower.includes("suggestion") || lower.includes("nitpick") || lower.includes("consider")) {
      currentSection = "suggestions";
    }

    // Extract issue lines (lines starting with -, *, or numbers)
    const issueMatch = line.match(/^\s*[-*•]\s+(.+)$/) || line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (issueMatch && currentSection) {
      issues[currentSection].push(issueMatch[1].trim());
    }
  }

  return issues;
}

// Create the MCP server
const server = new Server(
  {
    name: "codex-reviewer",
    version: "1.0.0",
  },
  {
    capabilities: {
      tools: {},
    },
  }
);

// Define available tools
server.setRequestHandler(ListToolsRequestSchema, async () => {
  return {
    tools: [
      {
        name: "codex_review",
        description: "Run a general code review using Codex. Returns review findings including correctness, readability, maintainability issues.",
        inputSchema: {
          type: "object",
          properties: {
            path: {
              type: "string",
              description: "File or directory path to review",
            },
            focus: {
              type: "string",
              description: "Optional focus area (e.g., 'error handling', 'naming', 'structure')",
            },
          },
          required: ["path"],
        },
      },
      {
        name: "codex_security_review",
        description: "Run a security-focused review using Codex. Checks for vulnerabilities, injection risks, auth issues, data exposure.",
        inputSchema: {
          type: "object",
          properties: {
            path: {
              type: "string",
              description: "File or directory path to review",
            },
            threat_model: {
              type: "string",
              description: "Optional threat model context (e.g., 'web app with user auth', 'CLI tool')",
            },
          },
          required: ["path"],
        },
      },
      {
        name: "codex_find_duplicates",
        description: "Find duplicate/copy-paste code using Codex. Returns locations and refactoring suggestions.",
        inputSchema: {
          type: "object",
          properties: {
            path: {
              type: "string",
              description: "File or directory path to scan",
            },
          },
          required: ["path"],
        },
      },
      {
        name: "codex_find_dead_code",
        description: "Find unused/dead code using Codex. Returns unreachable code, unused functions, orphan files.",
        inputSchema: {
          type: "object",
          properties: {
            path: {
              type: "string",
              description: "File or directory path to scan",
            },
          },
          required: ["path"],
        },
      },
      {
        name: "codex_verify_fixes",
        description: "Verify that previously identified issues have been fixed. Pass the original issues to check.",
        inputSchema: {
          type: "object",
          properties: {
            path: {
              type: "string",
              description: "File or directory path to verify",
            },
            original_issues: {
              type: "string",
              description: "Description of the original issues that were supposed to be fixed",
            },
          },
          required: ["path", "original_issues"],
        },
      },
      {
        name: "codex_test_coverage",
        description: "Analyze test coverage gaps using Codex. Identifies untested code paths and missing edge cases.",
        inputSchema: {
          type: "object",
          properties: {
            source_path: {
              type: "string",
              description: "Path to source code",
            },
            test_path: {
              type: "string",
              description: "Path to test files",
            },
          },
          required: ["source_path"],
        },
      },
      {
        name: "codex_dependency_audit",
        description: "Audit dependencies for security vulnerabilities, license issues, and bloat.",
        inputSchema: {
          type: "object",
          properties: {
            path: {
              type: "string",
              description: "Project root path",
            },
          },
          required: ["path"],
        },
      },
      {
        name: "codex_performance_review",
        description: "Review code for performance issues: O(n²) algorithms, memory leaks, N+1 queries, resource cleanup.",
        inputSchema: {
          type: "object",
          properties: {
            path: {
              type: "string",
              description: "File or directory path to review",
            },
            context: {
              type: "string",
              description: "Optional context (e.g., 'hot path', 'startup code', 'background job')",
            },
          },
          required: ["path"],
        },
      },
      {
        name: "codex_prompt",
        description: "Run a custom prompt through Codex. Use for any review task not covered by other tools.",
        inputSchema: {
          type: "object",
          properties: {
            prompt: {
              type: "string",
              description: "The prompt to send to Codex",
            },
            working_directory: {
              type: "string",
              description: "Working directory for Codex (defaults to current)",
            },
          },
          required: ["prompt"],
        },
      },
    ],
  };
});

// Handle tool calls
server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  try {
    let prompt;
    let cwd = args.working_directory || args.path || process.cwd();

    // If path is a file, use its directory
    if (cwd && !cwd.endsWith("/")) {
      const lastSlash = cwd.lastIndexOf("/");
      if (lastSlash > 0) {
        // Keep the full path for the prompt, but may use parent dir for cwd
      }
    }

    switch (name) {
      case "codex_review":
        prompt = `Review the code in ${args.path} for:
- Correctness: bugs, edge cases, error handling
- Readability: naming, structure, comments
- Maintainability: coupling, complexity, testability
${args.focus ? `Focus especially on: ${args.focus}` : ""}

Format your response with sections:
## BLOCKING (must fix)
## CONCERNS (should address)
## SUGGESTIONS (nice to have)
## WHAT'S GOOD (positive aspects)`;
        break;

      case "codex_security_review":
        prompt = `Security review of ${args.path}:

Check for:
- Input validation and sanitization
- Injection risks (SQL, command, XSS, path traversal)
- Authentication and authorization issues
- Sensitive data exposure (logs, errors, responses)
- Cryptography issues
- Dependency vulnerabilities
${args.threat_model ? `Threat model context: ${args.threat_model}` : ""}

Format with severity levels: CRITICAL, HIGH, MEDIUM, LOW
Include file:line references for each issue.`;
        break;

      case "codex_find_duplicates":
        prompt = `Find duplicate and copy-paste code in ${args.path}.

Look for:
- Exact code clones
- Similar code with renamed variables
- Logic that should be extracted to shared functions

For each duplicate found:
1. List all locations (file:line)
2. Show the duplicated code
3. Suggest how to refactor (extract function, create base class, etc.)

Format as a table: | Locations | Lines | Suggested Refactoring |`;
        break;

      case "codex_find_dead_code":
        prompt = `Find dead/unused code in ${args.path}.

Look for:
- Unreachable code (after return/throw)
- Unused functions and methods
- Unused variables and imports
- Unused files
- Commented-out code blocks
- Feature flags that are always on/off

For each finding, include:
- Location (file:line)
- Type (unreachable, unused function, etc.)
- Confidence level (high/medium/low)
- Safe to remove? (yes/verify/no)`;
        break;

      case "codex_verify_fixes":
        prompt = `Verify that these issues have been fixed in ${args.path}:

ORIGINAL ISSUES:
${args.original_issues}

For each original issue:
1. Check if it's been addressed
2. Mark as: FIXED, PARTIALLY FIXED, or NOT FIXED
3. If not fully fixed, explain what's still needed

Also check: did the fixes introduce any new issues?`;
        break;

      case "codex_test_coverage":
        prompt = `Analyze test coverage for ${args.source_path}${args.test_path ? ` (tests in ${args.test_path})` : ""}.

Identify:
- Functions/methods without test coverage
- Code paths not exercised by tests
- Edge cases not tested (nulls, empty, boundaries, errors)
- Missing assertion types (only happy path? no error cases?)

For each gap:
- Location in source code
- What's not tested
- Suggested test case`;
        break;

      case "codex_dependency_audit":
        prompt = `Audit dependencies in ${args.path}.

Check:
1. Security vulnerabilities (run npm audit, pip-audit, or equivalent)
2. License compatibility issues
3. Unused dependencies
4. Outdated dependencies with available updates
5. Heavy dependencies that could be replaced

Format:
## SECURITY VULNERABILITIES (by severity)
## LICENSE ISSUES
## UNUSED DEPENDENCIES
## UPDATE RECOMMENDATIONS`;
        break;

      case "codex_performance_review":
        prompt = `Performance review of ${args.path}${args.context ? ` (context: ${args.context})` : ""}.

Check for:
- O(n²) or worse algorithms in loops
- Unnecessary allocations or copies
- N+1 query patterns (database)
- Missing connection/resource cleanup
- Blocking operations that should be async
- Memory leaks (event listeners, closures, caches)
- Inefficient data structures

For each issue:
- Location (file:line)
- Problem description
- Performance impact (high/medium/low)
- Suggested fix`;
        break;

      case "codex_prompt":
        prompt = args.prompt;
        cwd = args.working_directory || process.cwd();
        break;

      default:
        return {
          content: [
            {
              type: "text",
              text: `Unknown tool: ${name}`,
            },
          ],
          isError: true,
        };
    }

    // Run Codex
    const result = await runCodex(prompt, { cwd });

    // Parse for structured issues if it's a review
    let parsedIssues = null;
    if (name.includes("review") || name === "codex_verify_fixes") {
      parsedIssues = parseReviewOutput(result.output);
    }

    // Build response
    const response = {
      success: result.success,
      output: result.output,
    };

    if (parsedIssues) {
      response.parsed_issues = parsedIssues;
      response.has_blocking_issues = parsedIssues.blocking.length > 0;
      response.total_issues =
        parsedIssues.blocking.length +
        parsedIssues.concerns.length +
        parsedIssues.suggestions.length;
    }

    if (result.stderr) {
      response.stderr = result.stderr;
    }

    return {
      content: [
        {
          type: "text",
          text: JSON.stringify(response, null, 2),
        },
      ],
    };
  } catch (error) {
    return {
      content: [
        {
          type: "text",
          text: JSON.stringify({
            success: false,
            error: error.message,
          }),
        },
      ],
      isError: true,
    };
  }
});

// Start the server
async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("Codex Reviewer MCP server running");
}

main().catch(console.error);
