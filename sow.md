# TraceMind — Technical Statement of Work (SOW) and Project Plan

**Document type:** Technical SOW, Architecture, and Implementation Plan  
**Project:** TraceMind  
**Version:** 1.0 — Proposed V1 baseline  
**Status:** Draft for technical review and approval  
**Primary objective:** Build an evidence-driven application observability and AI-assisted incident investigation platform.

---

## 1. Project Overview

### 1.1 What is TraceMind?

TraceMind is a developer-focused observability and incident investigation platform that helps engineering teams understand application failures, identify potential root causes, and investigate code changes associated with incidents.

It collects application logs and telemetry, organizes events into incidents, connects incidents to repository history and code relationships, retrieves relevant historical evidence, and uses an LLM to produce structured investigation reports.

TraceMind should help developers answer:

- What happened?
- When did it start?
- Which service, environment, or application was affected?
- Which release or code revision was running?
- What evidence is associated with the incident?
- Which code changes may be relevant?
- Have similar incidents occurred before?
- What are the plausible root causes?
- What should a developer investigate or test next?
- How confident are we, and what evidence is missing?

TraceMind must distinguish observed facts from inferred explanations. It must not claim a root cause has been verified unless the available evidence supports that conclusion.

### 1.2 Product Classification

TraceMind is best classified as a **full-stack observability and AI-assisted incident investigation platform with a Python backend**.

It includes middleware-like components for telemetry ingestion, event normalization, integration adapters, and communication between services. However, calling the entire product a middleware project would be incomplete because TraceMind also includes a data platform, incident investigation engine, repository intelligence layer, API, and developer dashboard.

### 1.3 Target Users

- Software developers
- Backend and platform engineers
- Site Reliability Engineers (SREs)
- DevOps and infrastructure engineers
- Engineering team leads
- Technical support and incident response teams

### 1.4 Core Design Principles

1. **Evidence first:** Every investigation conclusion should reference supporting evidence.
2. **Read-only by default:** V1 may inspect code and data but must not automatically modify production systems or application source code.
3. **Deterministic before generative:** Use normal code for parsing, validation, filtering, grouping, and other operations that do not require an LLM.
4. **Explicit uncertainty:** Distinguish facts, hypotheses, correlations, and verified resolutions.
5. **Traceability:** Record where evidence originated and how it was used.
6. **Modular architecture:** Keep ingestion, storage, repository analysis, investigation, and presentation separate.
7. **Security by design:** Redact secrets, validate tool inputs, restrict database access, and audit sensitive operations.
8. **Incremental delivery:** Build and test one working vertical slice at a time.
9. **Cost awareness:** Control Snowflake queries, LLM usage, storage, and background processing costs.
10. **Reproducibility:** Use synthetic applications and reproducible failures to evaluate the system.

---

## 2. Project Objectives

The initial version of TraceMind should:

1. Ingest structured application logs and selected telemetry events.
2. Normalize incoming events into a consistent internal schema.
3. Identify and group related error occurrences into incidents using deterministic rules.
4. Store operational application state in PostgreSQL.
5. Store and query appropriate telemetry and historical investigation data in Snowflake.
6. Associate deployments with actual Git commit SHAs when reliable deployment metadata is available.
7. Use Git history and diffs to identify relevant code changes.
8. Use Graphify or an equivalent verified code-analysis capability to represent code relationships and dependencies.
9. Retrieve historical incidents and supporting evidence relevant to a new incident.
10. Use Groq through a provider abstraction to generate structured, evidence-grounded investigation hypotheses.
11. Provide a dashboard for incidents, investigations, repositories, telemetry health, and findings.
12. Maintain audit records for investigations and tool usage.
13. Provide automated tests, reproducible demonstrations, documentation, and local development instructions.

### 2.1 Success Criteria

V1 is successful when a developer can submit or generate a known application failure, see the resulting incident, inspect the related telemetry and code changes, launch an investigation, and receive a structured report that cites real evidence and acknowledges uncertainty.

A successful demonstration must not depend on fabricated incident evidence or hardcoded root-cause conclusions.

---

## 3. Scope of Work

### 3.1 In Scope — V1

#### A. Project and Repository Management

- Register a project.
- Associate a Git repository with a project.
- Store repository metadata and relevant revisions.
- Track repository indexing status.
- Record commit SHAs and commit metadata.
- Inspect actual Git history and diffs.
- Connect deployments to commits when verified metadata exists.
- Integrate Graphify after verifying its installation, interface, output format, and suitability.

#### B. Telemetry Ingestion

- Accept structured JSON logs and selected telemetry events through an API.
- Validate incoming payloads.
- Normalize timestamps, service names, environments, severity levels, and event identifiers.
- Redact configured secrets and sensitive fields before persistence.
- Deduplicate events where appropriate.
- Associate events with projects and services.
- Track ingestion errors and processing status.
- Support synthetic event generation for local demonstrations.

#### C. Incident Detection and Management

- Detect eligible errors using deterministic rules.
- Group repeated occurrences using configurable fingerprints.
- Create and update incidents.
- Track severity, status, first occurrence, last occurrence, and occurrence count.
- Associate incidents with services, environments, deployments, and available code revisions.
- Support incident lifecycle states such as `open`, `investigating`, `resolved`, and `ignored`.
- Preserve the difference between an incident being marked resolved and a root cause being technically verified.

#### D. Repository and Code Change Intelligence

- Retrieve actual Git commits and diffs.
- Identify files changed between relevant revisions.
- Use Graphify, if verified, to retrieve code relationships and relevant dependency information.
- Connect repository evidence to incident investigation.
- Identify potentially relevant changes based on available timestamps, revisions, stack traces, and code relationships.
- Explain when a reliable release-to-commit mapping is unavailable.
- Avoid claiming that a nearby commit caused an incident merely because of temporal proximity.

#### E. Historical Knowledge Retrieval

- Query relevant historical telemetry and investigation records from Snowflake.
- Search for similar error patterns, incidents, services, and relevant historical resolutions.
- Retrieve relevant evidence using controlled, parameterized query templates.
- Record which historical records were used by an investigation.
- Distinguish historical suggestions from verified fixes applicable to the current incident.

#### F. AI-Assisted Investigation

- Provide a controlled investigation workflow.
- Gather evidence from registered tools.
- Retrieve relevant historical records.
- Ask the LLM to produce structured hypotheses.
- Include supporting and contradicting evidence.
- Identify missing evidence and recommended diagnostic steps.
- Suggest code areas and tests to inspect.
- Record model/provider metadata and investigation status.
- Validate LLM output against a defined schema.
- Retry or fail safely when output is invalid.
- Never give the LLM unrestricted shell, database, or production access.

#### G. Dashboard and API

- Project overview dashboard.
- Incident list and filters.
- Incident detail page.
- Investigation reports and evidence.
- Repository indexing and change status.
- Deployment and revision information.
- Ingestion and processing health.
- API endpoints for core operations.
- Clear loading, empty, success, and error states.

#### H. Quality and Delivery

- Automated backend and frontend tests.
- API documentation.
- Environment configuration documentation.
- Docker-based local development where appropriate.
- CI checks.
- Example application or reproducible failure scenarios.
- Security and limitation documentation.
- A working end-to-end demonstration.

### 3.2 Out of Scope — V1

The following are not part of the initial release unless explicitly approved later:

- Automatic production code modification.
- Automatic deployment or rollback.
- Autonomous execution of arbitrary shell commands.
- Unrestricted LLM-generated SQL execution.
- Full replacement of commercial observability platforms.
- Guaranteed automatic root-cause identification.
- Comprehensive support for every programming language and telemetry vendor.
- A large-scale distributed microservices architecture.
- Enterprise SSO, complex RBAC, billing, and multi-tenant compliance features.
- Mobile applications.
- Automatic remediation without human approval.
- Full OpenTelemetry platform replacement.
- Real-time distributed tracing analysis beyond the telemetry types actually implemented and tested.

These may become future roadmap items after V1 demonstrates value and the necessary security controls are designed.

---

## 4. Proposed Technical Architecture

### 4.1 High-Level Architecture

```text
                 Developers / Engineering Teams
                              |
                              v
                  TraceMind Web Dashboard
                  React + TypeScript
                              |
                              v
                       FastAPI Backend
                              |
               +--------------+--------------+
               |              |              |
               v              v              v
        Project & API     Incident       Investigation
        Management        Management      Coordinator
                              |              |
                              |              v
                              |       Controlled Tool Registry
                              |              |
               +--------------+--------------+----------------+
               |              |              |                |
               v              v              v                v
        Telemetry         Git Adapter     Graphify Adapter  History
        Pipeline          & Diff Engine   / Code Analysis   Retrieval
               |              |              |                |
               v              v              v                v
        Normalization     Repository      Code Relations   Snowflake
        & Redaction       History         & Dependencies   Queries
               |
               +------------------+
                                  |
                   +--------------+--------------+
                   |                             |
                   v                             v
               PostgreSQL                    Snowflake
            Operational State             Telemetry & History

                                  |
                                  v
                          Groq LLM Provider
                                  |
                                  v
                      Validated Investigation Report
                                  |
                                  v
                         Dashboard / API Response
```

This diagram represents logical components. They do not all need to run as independent services in V1.

### 4.2 Architectural Recommendation

Start with a **modular monolith plus a separate background worker process**.

The API, incident management, investigation coordinator, and integration adapters can initially live in one Python codebase with clearly defined module boundaries. Long-running repository indexing and investigation tasks should execute through a background worker rather than blocking HTTP requests.

A distributed architecture should be introduced only when real workload, reliability, or scaling requirements justify it.

### 4.3 Proposed Technology Stack

| Layer | Proposed technology | Responsibility |
|---|---|---|
| Frontend | React, TypeScript, Vite | Developer dashboard |
| UI styling | Tailwind CSS and a suitable component library | Consistent UI components |
| API | Python, FastAPI, Pydantic | API, validation, orchestration |
| Operational database | PostgreSQL | Projects, incidents, jobs, feedback, audit records |
| Analytics and telemetry | Snowflake | Queryable telemetry and historical evidence |
| Repository history | Git | Authoritative commits, branches, diffs |
| Code relationship analysis | Graphify, subject to verification | Derived code structure and relationships |
| Telemetry conventions | Structured JSON logs and selected OpenTelemetry data | Event collection and normalization |
| LLM | Groq API behind a provider interface | Hypothesis generation and report synthesis |
| Background processing | Separate Python worker initially | Indexing and investigations |
| Testing | pytest, HTTPX, Vitest | Backend, API, and frontend testing |
| Code quality | Ruff and a suitable static type checker | Linting and type checks |
| Packaging | Docker and Docker Compose, where appropriate | Reproducible local environment |
| CI | GitHub Actions or an equivalent CI system | Automated quality gates |

This stack is a proposal, not a mandate to install every dependency immediately. Claude must inspect the current repository and verify package compatibility before making changes.

### 4.4 Technology Selection Rules

- PostgreSQL is the source of truth for operational state.
- Snowflake is used for suitable analytical and historical data, not as a replacement for every operational database table.
- Git is the authoritative source for committed source history.
- Graphify output is derived information and must not override actual source code or Git history.
- Groq is an LLM provider, not the source of truth for incidents or code facts.
- Provider-specific LLM logic must remain behind an interface so the model can be changed later.
- OpenTelemetry should be used where its conventions provide value, without attempting to implement all of its specifications in V1.
- Dependency versions, SDK interfaces, Graphify capabilities, and Snowflake compatibility must be verified against current documentation.

---

## 5. Data Architecture

### 5.1 PostgreSQL — Operational State

PostgreSQL should hold data required for normal application operations, including:

- Users or local development identities, if authentication is implemented.
- Projects.
- Repository registrations.
- Repository indexing jobs and status.
- Code revision metadata.
- Deployment records.
- Incident records and lifecycle state.
- Incident occurrence references.
- Investigation jobs and statuses.
- Hypotheses and report metadata.
- Evidence references.
- Resolution records.
- User feedback.
- Audit events.

### 5.2 Snowflake — Telemetry and Historical Evidence

Snowflake should hold appropriate analytical and historical data, including:

- Normalized telemetry events.
- Historical event data.
- Selected deployment and revision metadata for analytical joins.
- Historical incident summaries or investigation evidence where justified.
- Data needed for historical pattern analysis and incident comparison.

The exact table layout, clustering strategy, retention policy, warehouse size, and query design must be based on expected data volume and cost.

Do not duplicate every PostgreSQL record in Snowflake without a clear analytical purpose.

### 5.3 Data Model — Initial Logical Entities

The initial logical data model should include:

1. `Project`
2. `Repository`
3. `CodeRevision`
4. `Deployment`
5. `TelemetryEvent`
6. `Incident`
7. `IncidentOccurrence`
8. `Investigation`
9. `Hypothesis`
10. `EvidenceReference`
11. `Resolution`
12. `Feedback`
13. `AuditEvent`

These are conceptual entities. They do not require one physical table each. Final schema decisions should follow repository inspection and an explicit data-model design.

### 5.4 Data Integrity Rules

- Use stable identifiers for projects, incidents, and investigations.
- Store timestamps in a consistent timezone-aware representation, preferably UTC.
- Preserve original event timestamps separately from ingestion timestamps when available.
- Keep event source metadata and processing status.
- Avoid storing secrets or unnecessary personal data.
- Use explicit relationships between incidents, deployments, revisions, and evidence.
- Preserve provenance for each investigation claim.
- Track unknown or missing information explicitly instead of manufacturing values.
- Define retention and deletion behavior for both PostgreSQL and Snowflake.

---

## 6. Telemetry Ingestion and Normalization

### 6.1 Input Contract

V1 should accept structured JSON events through an authenticated or appropriately restricted API.

An illustrative event:

```json
{
  "project_id": "project-demo",
  "service": "payments-api",
  "environment": "development",
  "timestamp": "2026-10-28T10:15:30Z",
  "severity": "ERROR",
  "message": "Database connection failed",
  "error_type": "ConnectionError",
  "trace_id": null,
  "span_id": null,
  "release": "1.2.0",
  "commit_sha": null,
  "attributes": {
    "endpoint": "/payments",
    "retry_count": 3
  }
}
```

This is an illustrative payload, not a final API contract.

### 6.2 Required Processing Steps

1. Validate the incoming event.
2. Confirm that the project and service are registered or permitted.
3. Normalize severity, timestamps, and field names.
4. Redact secrets and configured sensitive fields.
5. Apply payload-size and rate limits.
6. Generate or validate an event identifier.
7. Deduplicate where an appropriate key is available.
8. Persist or enqueue the normalized event.
9. Update ingestion metrics and processing status.
10. Return a clear success or validation error response.

### 6.3 Reliability Requirements

- Invalid payloads must not silently become valid events.
- Duplicate delivery must not create uncontrolled duplicate incidents.
- Failed ingestion and processing must be observable.
- Retries must be bounded and safe.
- Large payloads must be rejected or truncated according to documented rules.
- Ingestion must not block on a complete LLM investigation.
- Sensitive data must be redacted before being sent to an external LLM provider.

### 6.4 Telemetry Boundaries

V1 must clearly document which signal types are supported.

Structured logs are the initial required signal. Metrics, spans, trace correlation, and additional OpenTelemetry integration may be added incrementally after their schemas and use cases are defined.

The system must not display fabricated trace data when trace identifiers or spans are absent.

---

## 7. Incident Detection and Lifecycle

### 7.1 Incident Creation

The initial detection engine should use deterministic rules, such as:

- Error severity.
- Exception type.
- Normalized message or stack-trace fingerprint.
- Service and environment.
- Occurrence frequency.
- Time window.

These rules should be configurable and testable.

### 7.2 Incident Grouping

Repeated occurrences may be grouped using a stable fingerprint derived from suitable event fields.

The implementation must account for:

- Message variation.
- Stack traces.
- Different services and environments.
- Missing exception information.
- Duplicate event delivery.
- Changes in grouping rules.

Grouping should avoid merging unrelated failures solely because they contain similar words.

### 7.3 Incident Fields

An incident should include, where available:

- Incident ID.
- Project and service.
- Environment.
- Severity.
- Status.
- Fingerprint.
- First-seen timestamp.
- Last-seen timestamp.
- Occurrence count.
- Related deployment.
- Related commit SHA.
- Summary.
- Investigation status.
- Evidence references.
- Resolution metadata.

### 7.4 Resolution Semantics

TraceMind must distinguish:

- **Incident closed:** A user or workflow has changed the incident's lifecycle status.
- **Hypothesis supported:** Evidence is consistent with a possible cause.
- **Resolution proposed:** A potential corrective action has been suggested.
- **Resolution verified:** There is explicit evidence that the corrective action addressed the failure.

These states must not be treated as interchangeable.

---

## 8. Repository Intelligence and Graphify

### 8.1 Purpose

Repository intelligence helps TraceMind connect an incident with the code and changes that may be relevant to the failure.

Git provides the authoritative committed history. Graphify, if suitable and available, provides derived code relationships that may improve navigation and investigation.

### 8.2 Required Capabilities

- Register a repository.
- Inspect repository metadata.
- Retrieve commit history.
- Compare actual Git revisions.
- Identify changed files.
- Record revision identifiers.
- Index code relationships where supported.
- Track indexing time, status, and errors.
- Relate incident evidence to changed files and dependencies.
- Expose the source of every suggested code relationship.

### 8.3 Graphify Verification Gate

Before implementation, Claude must verify:

- Which Graphify tool or package is intended.
- Whether it supports the target languages.
- Its installation and runtime requirements.
- Its input and output formats.
- Whether it creates persistent files or indexes.
- How updates and incremental indexing work.
- Whether the current repository can use it directly.
- Whether its output can be safely consumed by TraceMind.

Do not invent Graphify APIs or assume that a particular command, package, or MCP integration exists.

If Graphify is unavailable or unsuitable, propose a documented fallback, such as a limited language-aware parser or repository metadata index. Do not silently claim that the fallback is equivalent to Graphify.

### 8.4 Release-to-Commit Association

TraceMind should associate a deployment with a Git commit SHA when trustworthy metadata is available.

Possible evidence includes:

- Deployment records.
- Build metadata.
- Release manifests.
- Explicit commit SHA in telemetry.
- CI/CD metadata.

A timestamp-based estimate may be presented as a weak lead, but it must not be represented as a verified deployment-to-commit mapping.

If the mapping is unknown, the report must say so.

### 8.5 Change-Relevance Analysis

The system may rank code changes using:

- Temporal proximity to the incident.
- A verified deployment-to-commit mapping.
- Files referenced by stack traces.
- Relevant code dependencies.
- Error type and message.
- Historical incidents involving the same files or components.

A relevance score is a prioritization aid, not proof of causation. The system must expose the evidence behind its ranking.

---

## 9. Snowflake Integration and Historical Retrieval

### 9.1 Responsibilities

Snowflake should provide historical data that helps TraceMind compare incidents, inspect event patterns, and retrieve relevant evidence.

### 9.2 Implementation Requirements

- Use the appropriate supported Snowflake SDK or connector.
- Keep credentials in environment variables or a suitable secrets manager.
- Use least-privilege roles.
- Prefer parameterized queries and approved query templates.
- Apply query timeouts and bounded result sizes.
- Restrict warehouse usage and avoid unnecessary full-table scans.
- Record query metadata and investigation references.
- Handle connection failures, timeouts, and missing data.
- Test queries against synthetic or explicitly approved data.
- Document local development setup and required permissions.

### 9.3 Safe Querying

The LLM must not receive unrestricted ability to generate and execute arbitrary SQL.

Instead, implement a controlled retrieval layer with named operations such as:

- Search events for an incident fingerprint.
- Retrieve events for a service and time window.
- Find similar historical incidents.
- Retrieve previously documented resolutions.
- Retrieve telemetry for a verified deployment or revision.

The backend validates inputs, enforces limits, executes approved queries, and returns structured results.

### 9.4 Cost and Retention

The implementation should document:

- Expected query patterns.
- Retention assumptions.
- Data volume assumptions.
- Warehouse usage.
- Query limits.
- Potential cost risks.
- Data cleanup procedures.

Exact cost targets must be established after the expected workload and account configuration are known.

---

## 10. AI Investigation Engine

### 10.1 Workflow

A typical investigation should follow this sequence:

1. Receive an incident ID.
2. Validate the incident and project permissions.
3. Retrieve incident occurrences and normalized telemetry.
4. Retrieve available deployment and commit metadata.
5. Inspect relevant Git history and diffs.
6. Retrieve code relationships from Graphify or the verified alternative.
7. Retrieve similar historical incidents from Snowflake.
8. Assemble a bounded, structured evidence package.
9. Redact sensitive information and label untrusted content.
10. Ask the configured LLM provider to generate structured hypotheses.
11. Validate the response against a schema.
12. Store the report and evidence references.
13. Display findings and uncertainty in the dashboard.
14. Allow users to provide feedback or record a verified resolution.

### 10.2 LLM Responsibilities

The LLM may:

- Summarize observed symptoms.
- Compare evidence across sources.
- Generate ranked root-cause hypotheses.
- Explain why each hypothesis may fit the evidence.
- Identify contradicting evidence.
- Identify missing information.
- Suggest targeted diagnostic steps.
- Recommend tests or code areas to inspect.
- Summarize similar historical incidents.

The LLM must not invent events, commits, files, stack traces, database results, or historical resolutions.

### 10.3 Proposed Investigation Report

```json
{
  "incident_id": "incident-example",
  "summary": "Database connection failures occurred in payments-api.",
  "observed_facts": [
    {
      "statement": "Connection failures were recorded during the incident window.",
      "evidence_ids": ["event-101", "event-102"]
    }
  ],
  "hypotheses": [
    {
      "title": "Database connection capacity may have been exhausted",
      "confidence": "medium",
      "supporting_evidence": ["event-101"],
      "contradicting_evidence": [],
      "missing_evidence": [
        "Database connection pool metrics"
      ],
      "recommended_checks": [
        "Inspect connection pool utilization during the incident window."
      ]
    }
  ],
  "related_code_changes": [],
  "historical_matches": [],
  "limitations": [
    "The deployment-to-commit mapping has not been verified."
  ],
  "recommended_next_steps": [
    "Collect database connection pool metrics."
  ],
  "resolution_status": "unverified"
}
```

This schema is illustrative and should be finalized before the investigator implementation begins.

### 10.4 Confidence and Evidence Rules

- Confidence values must use a documented scale.
- Every factual claim should reference one or more evidence records when possible.
- Missing evidence must be stated explicitly.
- Contradicting evidence must not be hidden.
- Correlation must not be presented as causation.
- Historical fixes must not automatically be assumed applicable to a new incident.
- A generated report must be marked as unverified until the relevant resolution is confirmed.

### 10.5 Provider Abstraction

Create an LLM provider interface so the application does not depend on Groq-specific behavior throughout the codebase.

The interface should cover:

- Request construction.
- Model configuration.
- Structured output handling.
- Timeouts and retries.
- Error normalization.
- Usage and cost metadata, where available.
- Safe logging without exposing secrets or sensitive prompt content.

Verify current model availability, API compatibility, structured-output support, and rate limits before selecting a concrete model.

### 10.6 Investigation Tool Registry

All investigation tools must be explicitly registered and validated.

Each tool should define:

- Name and purpose.
- Input schema.
- Output schema.
- Permission requirements.
- Timeout.
- Result-size limits.
- Error behavior.
- Audit behavior.
- Whether the tool is read-only.

The model may request a registered operation, but the backend must validate and execute it. The model must not directly bypass the registry.

---

## 11. API Design

The following endpoints are proposed starting points. They are not a final contract; adjust them after inspecting the existing repository.

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/api/v1/projects` | Create a project |
| GET | `/api/v1/projects` | List accessible projects |
| GET | `/api/v1/projects/{project_id}` | Retrieve project details |
| POST | `/api/v1/projects/{project_id}/repositories` | Register a repository |
| GET | `/api/v1/projects/{project_id}/repositories` | List project repositories |
| POST | `/api/v1/telemetry/events` | Submit telemetry events |
| GET | `/api/v1/incidents` | List and filter incidents |
| GET | `/api/v1/incidents/{incident_id}` | Retrieve incident details |
| POST | `/api/v1/incidents/{incident_id}/investigations` | Start an investigation |
| GET | `/api/v1/investigations/{investigation_id}` | Retrieve investigation status and report |
| POST | `/api/v1/incidents/{incident_id}/feedback` | Submit incident or investigation feedback |
| GET | `/api/v1/projects/{project_id}/deployments` | List deployment records |
| GET | `/api/v1/projects/{project_id}/repositories/{repository_id}/changes` | Retrieve repository changes |
| GET | `/api/v1/projects/{project_id}/repositories/{repository_id}/status` | Retrieve repository indexing status |
| GET | `/health` | Basic application health |
| GET | `/ready` | Dependency and readiness checks |

### API Requirements

- Use Pydantic request and response models.
- Return consistent error structures.
- Validate identifiers and permissions.
- Use pagination for list endpoints.
- Support filtering and sorting where appropriate.
- Avoid exposing credentials, internal stack traces, or sensitive configuration.
- Return a job or investigation identifier for long-running work.
- Provide an API contract through FastAPI's generated OpenAPI documentation.
- Avoid implementing endpoints that do not yet have a clear use case.

---

## 12. Dashboard and UI Requirements

TraceMind is not a backend-only library. The product should include a web interface for developers to inspect incidents and investigation results.

The dashboard should be information-dense enough for engineering use while keeping the most important operational information easy to find.

### 12.1 Global Navigation

The UI should provide navigation for:

- Overview
- Incidents
- Investigations
- Repositories
- Deployments
- Settings, if required

Project selection should be visible when multiple projects are supported.

### 12.2 Overview Dashboard

The overview should show:

- Total incidents within a selected time window.
- Open and investigating incidents.
- Critical or high-severity incidents.
- Incident frequency over time.
- Most affected services.
- Recent incidents.
- Telemetry ingestion health.
- Repository indexing status.
- Recent deployments and available commit associations.
- Recent investigation activity.

Use real data from the API. Empty states should explain how to create or ingest the first project, event, or incident.

Do not add decorative charts that have no clear operational purpose.

### 12.3 Incident List

Provide:

- Incident title or normalized error summary.
- Severity.
- Service and environment.
- First-seen and last-seen timestamps.
- Occurrence count.
- Incident status.
- Investigation status.
- Available deployment or revision information.

Allow filtering by severity, service, environment, status, and time window where supported.

### 12.4 Incident Detail

The detail page should include:

- Incident summary and status.
- First-seen and last-seen timestamps.
- Occurrence count.
- Event and stack-trace evidence, where available.
- Timeline of relevant events.
- Deployment and commit information, including uncertainty.
- Related code changes.
- Investigation history.
- Suggested diagnostic actions.
- Resolution status and feedback.

The user should be able to start an investigation and see its progress.

### 12.5 Investigation Detail

Display:

- Investigation status.
- Evidence collected.
- Observed facts.
- Ranked hypotheses.
- Supporting and contradicting evidence.
- Missing evidence.
- Related code changes and historical incidents.
- Recommended next steps.
- LLM/provider metadata when useful.
- Limitations and uncertainty.
- User feedback or resolution verification.

### 12.6 Repository Page

Display:

- Repository details.
- Last indexed revision.
- Indexing status and errors.
- Recent commits.
- Changed files.
- Graphify or alternative index status.
- Repository-related incidents.

### 12.7 UI Design Principles

- Use a clean developer-tool aesthetic.
- Prioritize readability over decorative styling.
- Use consistent severity and status indicators.
- Distinguish factual evidence from AI-generated hypotheses.
- Provide useful empty, loading, retry, and error states.
- Make timestamps and timezones clear.
- Avoid implying real-time updates unless they are implemented.
- Ensure the interface is usable at common desktop widths.
- Add responsive behavior where practical.
- Keep visualizations tied to actual API data.

---

## 13. Security, Privacy, and Governance

### 13.1 Threat Model

The implementation must consider:

- Secrets appearing in logs.
- Malicious or malformed telemetry payloads.
- Prompt injection embedded in logs, source code, or repository documentation.
- Unauthorized access to projects or repositories.
- Arbitrary SQL execution.
- Unsafe shell command execution.
- Compromised LLM output.
- Excessive query or token consumption.
- Sensitive information leaking into application logs.
- Dependency vulnerabilities.
- Incorrect claims about deployments or root causes.

### 13.2 Required Controls

- Validate every external input.
- Enforce payload-size limits.
- Redact secrets before persistence and before external LLM calls.
- Use least-privilege database credentials.
- Keep secrets out of source control.
- Use controlled, read-only investigation tools.
- Never execute model-generated shell commands automatically.
- Never execute arbitrary model-generated SQL.
- Treat logs, source files, and retrieved documents as untrusted data, not as instructions.
- Apply timeouts and bounded result sizes.
- Audit sensitive tool operations.
- Avoid exposing internal exception details to users.
- Document limitations and residual risks.

### 13.3 Human Approval Boundary

V1 is a read-only investigation system.

Any future action that modifies code, configuration, infrastructure, or production state must require a separate design, explicit authorization, appropriate safeguards, and human approval. It must not be introduced implicitly as part of the initial investigation workflow.

---

## 14. Testing and Evaluation

### 14.1 Backend Testing

Test:

- Request validation.
- Event normalization.
- Secret redaction.
- Deduplication.
- Incident fingerprinting and grouping.
- Incident lifecycle transitions.
- Repository adapter behavior.
- Commit and diff retrieval.
- Graphify adapter behavior or documented fallback.
- Snowflake query construction and result handling.
- Tool registry input validation.
- Investigation report schema validation.
- Error handling, timeouts, and retries.
- Authorization and audit boundaries.

Use mocks for external services where appropriate, but include integration tests against actual supported interfaces when feasible.

### 14.2 Frontend Testing

Test:

- Navigation.
- Incident list filtering.
- Incident detail rendering.
- Investigation status updates.
- Evidence and hypothesis display.
- Loading and empty states.
- API error states.
- User feedback submission.
- Accessibility of key controls.

### 14.3 End-to-End Testing

Create a small sample application that generates reproducible failures, such as:

- A controlled exception.
- A simulated dependency timeout.
- A configuration error.
- A regression introduced by a known code change.

The test should demonstrate the full path from event ingestion to incident creation, investigation, and dashboard presentation.

### 14.4 Evaluation Metrics

Measure, where applicable:

- Event ingestion success rate.
- Incident grouping accuracy.
- Detection recall on known test failures.
- False-positive rate.
- Deployment-to-commit association accuracy.
- Evidence traceability.
- Investigation completion rate.
- Investigation latency.
- Invalid structured-output rate.
- LLM token usage and estimated cost.
- Snowflake query cost and duration.
- Recovery behavior when dependencies fail.

Set numeric performance targets after establishing a baseline. Do not claim a target has been achieved without measured results.

### 14.5 Definition of Done

A feature is complete only when:

1. Its behavior is implemented.
2. Its inputs and outputs are documented.
3. Its failure modes are handled.
4. Relevant automated tests pass.
5. Security implications have been reviewed.
6. Its UI or API behavior is demonstrable.
7. Its data provenance is preserved where applicable.
8. Its limitations are documented.
9. It passes the agreed acceptance criteria.

---

## 15. Implementation Plan

The proposed timeline is approximately **8–10 weeks of focused solo development**. This is an initial estimate, not a guaranteed delivery date. Actual duration depends on the current repository state, integration complexity, available infrastructure, and the results of early technical verification.

### Milestone 0 — Discovery and Foundation

**Estimated duration:** Week 1

Tasks:

- Inspect the existing TraceMind repository.
- Read existing scope documents and project skills.
- Identify implemented features, incomplete modules, and technical debt.
- Confirm the development environment.
- Verify the intended Graphify integration.
- Verify Snowflake account and connector requirements.
- Verify Groq API and model availability.
- Define architecture decisions and module boundaries.
- Define initial data schemas and API contracts.
- Create a threat model.
- Create a reproducible demo scenario.
- Establish linting, formatting, and test commands.

Deliverables:

- Repository discovery report.
- Existing-versus-proposed scope comparison.
- Architecture decision records.
- Dependency verification report.
- Initial data model.
- Initial API contract.
- Risk register.
- Approved milestone backlog.

Acceptance criteria:

- Existing code and functionality are documented.
- Major unknowns are identified.
- Proposed dependencies have been verified or marked unresolved.
- The first end-to-end test scenario is defined.
- No large-scale refactor begins before the discovery findings are reviewed.

### Milestone 1 — Core Backend and Telemetry Ingestion

**Estimated duration:** Weeks 2–3

Tasks:

- Implement or refine project registration.
- Define the normalized telemetry schema.
- Implement telemetry validation and redaction.
- Implement ingestion endpoint.
- Add persistence and processing status.
- Add structured logging and health checks.
- Add deterministic incident fingerprinting.
- Add tests for invalid and duplicate events.

Deliverables:

- Working ingestion API.
- Normalized event representation.
- Initial operational persistence.
- Basic incident creation.
- Automated tests and API documentation.

Acceptance criteria:

- A valid sample event is accepted and stored.
- Invalid payloads return clear errors.
- Sensitive test values are redacted.
- Repeated events are handled according to documented deduplication rules.
- A known error produces a reproducible incident.
- No LLM call is required for deterministic ingestion or grouping.

### Milestone 2 — Git and Repository Intelligence

**Estimated duration:** Weeks 3–4

Tasks:

- Register repositories.
- Implement a safe Git adapter.
- Retrieve commits and diffs.
- Record revision metadata.
- Implement repository indexing status.
- Verify and integrate Graphify or implement an explicitly documented fallback.
- Connect repository evidence to incidents.

Deliverables:

- Repository API.
- Commit and diff retrieval.
- Indexing status.
- Initial code relationship retrieval.
- Tests for missing revisions and indexing failures.

Acceptance criteria:

- The system can retrieve actual commits and diffs from a test repository.
- Every displayed change references a real revision or file.
- Graphify behavior is based on verified integration results.
- Unknown release-to-commit mappings remain explicitly unknown.

### Milestone 3 — Snowflake Historical Retrieval

**Estimated duration:** Weeks 4–5

Tasks:

- Finalize the telemetry and history storage design.
- Configure Snowflake connectivity.
- Create the minimum required schema and tables.
- Implement approved query templates.
- Add time-window and result-size limits.
- Add error handling and query metadata.
- Test historical retrieval using controlled sample data.

Deliverables:

- Working Snowflake integration.
- Documented data model.
- Safe historical retrieval interface.
- Cost and retention notes.
- Integration tests where infrastructure is available.

Acceptance criteria:

- A known query retrieves the expected sample records.
- Query parameters are validated.
- Results are bounded.
- Credentials are not hardcoded.
- Failure and timeout cases are handled.
- The application remains usable when Snowflake is unavailable, with an appropriate degraded state.

### Milestone 4 — Incident Lifecycle and Evidence Assembly

**Estimated duration:** Weeks 5–6

Tasks:

- Finalize incident lifecycle behavior.
- Link events to incidents.
- Link incidents to available deployments and revisions.
- Assemble evidence packages.
- Retrieve similar historical incidents.
- Define evidence identifiers and provenance.
- Implement investigation job state.

Deliverables:

- Incident detail API.
- Incident timeline.
- Evidence package format.
- Historical matching capability.
- Investigation job model.

Acceptance criteria:

- Incident details show real associated events.
- Evidence references resolve to source records.
- Missing metadata is represented explicitly.
- Similarity results are not presented as proof of common causation.

### Milestone 5 — AI Investigation Engine

**Estimated duration:** Weeks 6–8

Tasks:

- Implement the provider abstraction.
- Integrate the verified Groq API.
- Implement the controlled tool registry.
- Define and validate the report schema.
- Generate evidence-grounded hypotheses.
- Add retries, timeouts, and failure handling.
- Add audit records.
- Add a small evaluation dataset of known incidents.

Deliverables:

- Working investigation coordinator.
- LLM provider adapter.
- Validated structured reports.
- Investigation history.
- Initial evaluation results.

Acceptance criteria:

- A known incident can trigger an investigation.
- Reports reference real evidence IDs.
- Invalid model output is rejected or safely retried.
- Missing evidence and uncertainty are represented.
- Arbitrary shell commands and unrestricted SQL cannot be executed by the LLM.
- External model failures do not corrupt incident records.

### Milestone 6 — Dashboard and End-to-End Release

**Estimated duration:** Weeks 8–10

Tasks:

- Implement the overview dashboard.
- Implement incident list and filters.
- Implement incident detail and timeline.
- Implement investigation report views.
- Implement repository and deployment status views.
- Add loading, empty, and error states.
- Add end-to-end tests.
- Improve documentation.
- Prepare a reproducible demonstration.

Deliverables:

- Working frontend.
- Integrated API.
- End-to-end demo.
- Setup and configuration documentation.
- Test and evaluation summary.
- V1 limitations and roadmap.

Acceptance criteria:

- A developer can navigate from the overview to an incident and its investigation.
- Displayed findings correspond to actual backend responses.
- Evidence and hypotheses are visually distinguished.
- Failures and missing integrations are visible.
- The documented setup works in a clean development environment, subject to documented external credentials.
- Automated quality checks pass.

---

## 16. Project Risks and Mitigations

| Risk | Potential impact | Mitigation |
|---|---|---|
| Graphify integration is incompatible or unclear | Delayed repository intelligence | Verify early; define a limited fallback |
| Snowflake cost or access limitations | Delayed historical retrieval | Start with minimal schemas and bounded queries |
| LLM hallucination | Incorrect incident conclusions | Require evidence IDs, schema validation, and uncertainty |
| Incorrect deployment mapping | Misleading change analysis | Use verified metadata and explicitly represent unknowns |
| Sensitive log data | Data exposure | Redact before persistence and external model calls |
| Prompt injection through logs or code | Unsafe model behavior | Treat retrieved content as untrusted data |
| Excessive architecture complexity | Slower delivery | Start with a modular monolith and a separate worker |
| Large data volumes | Slow or costly queries | Apply retention, filtering, limits, and indexing strategies |
| API or provider changes | Integration failures | Use adapters and verify current documentation |
| False incident grouping | Incorrect operational conclusions | Use deterministic, tested fingerprints and feedback |
| Missing integration credentials | Blocked end-to-end testing | Support mocks and synthetic data while documenting limitations |

---

## 17. Deliverables

The V1 project should produce:

1. Technical architecture and decisions.
2. Python backend with documented API contracts.
3. Telemetry ingestion and normalization.
4. Incident detection and lifecycle management.
5. PostgreSQL operational schema.
6. Snowflake integration and historical retrieval.
7. Git history and diff integration.
8. Verified Graphify integration or documented alternative.
9. AI investigation engine with a controlled tool registry.
10. Structured investigation report schema.
11. React and TypeScript dashboard.
12. Automated unit, integration, and end-to-end tests.
13. Reproducible demonstration scenarios.
14. Environment and setup documentation.
15. Security and limitations documentation.
16. Evaluation results and known issues.
17. V1 roadmap for future enhancements.

---

## 18. Future Roadmap — After V1

Potential later features include:

- Broader OpenTelemetry integration.
- More advanced metrics and trace correlation.
- Additional LLM providers.
- Improved incident similarity and ranking.
- More advanced code relationship analysis.
- Notifications and incident collaboration.
- Human-approved remediation proposals.
- Pull-request suggestions with explicit user approval.
- Additional repository and deployment providers.
- More advanced access control and multi-tenant support.
- Scalable distributed background processing.
- Evaluation datasets and regression tests for investigation quality.

These features should be prioritized only after V1's core workflow is reliable.

---

## 19. Instructions for Claude — Implementation Governance

Claude must treat this document as the proposed V1 scope and planning baseline, not permission to implement everything at once.

### 19.1 First Action: Discovery

Before editing code, Claude must:

1. Read this SOW completely.
2. Inspect the current TraceMind repository.
3. Read existing project documentation, agent instructions, and skills.
4. Identify what is already implemented.
5. Compare current implementation against this SOW.
6. Verify dependencies and integration capabilities using reliable documentation or installed interfaces.
7. Identify architectural conflicts, risks, missing credentials, and unresolved decisions.
8. Propose a prioritized Milestone 0 task list.
9. Define acceptance criteria and tests for each proposed task.
10. Present findings for review and approval.

**Do not edit or create implementation files until the discovery report and initial task plan have been reviewed and approved.**

### 19.2 Implementation Rules

After approval:

- Implement one milestone or a small approved vertical slice at a time.
- Inspect existing code before creating replacements.
- Avoid unnecessary rewrites.
- Follow the existing repository conventions where they remain appropriate.
- Explain why a dependency or architectural change is needed.
- Verify package names, APIs, and model availability before relying on them.
- Do not invent completed features, test results, provider capabilities, or data.
- Run relevant tests and report the actual results.
- Update documentation when behavior changes.
- Keep secrets out of source control.
- Maintain read-only investigation boundaries.
- Do not add autonomous production remediation to V1.
- Clearly label incomplete, mocked, simulated, and unverified functionality.
- Ask for approval before major architecture changes or scope expansion.

### 19.3 Required Progress Reports

At the end of each milestone, report:

- What was implemented.
- Files and modules changed.
- Architecture decisions made.
- Tests executed and actual outcomes.
- Integrations verified.
- Known limitations and unresolved risks.
- Any changes to scope or estimates.
- Proposed next milestone and acceptance criteria.

### 19.4 Definition of Technical Completion

The project is not complete simply because code has been generated.

A milestone is complete when the implementation works, relevant tests have been run, the acceptance criteria are met, documentation is updated, and limitations are disclosed.

---

## 20. Immediate Next Step

Begin with **Milestone 0 — Discovery and Foundation**.

The first output should be a technical discovery report containing:

1. Current repository structure and architecture.
2. Existing functionality mapped to the SOW.
3. Missing or incomplete functionality.
4. Dependency and integration verification findings.
5. Technical risks and unresolved decisions.
6. Proposed architecture adjustments, if needed.
7. A prioritized implementation backlog.
8. Acceptance criteria and tests for the first vertical slice.
9. Estimated effort and dependencies.
10. Explicit questions requiring human decisions.

Do not begin full implementation until this report and the first milestone plan are approved.
