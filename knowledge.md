Context
This repository contains an iOS application. You are acting as the primary AI development assistant for this project.

# Documentation & SDLC (Mandatory)
* **Primary Directive:** Before initiating any new feature, planning an architecture change, or generating foundational code, you must read and adhere to the Software Development Life Cycle (SDLC) documentation located in the `docs/` directory.
* **Compliance:** All file structures, testing protocols, and design patterns must strictly align with the guidelines specified in the `docs/` folder. Do not deviate from these established standards.
* **Architecture doc:** `docs/architecture.md` is the binding implementation contract for this app (scope, stack, module layout, API contract). Keep it current when the design changes.

# Tech Stack & Coding Standards
* **Platform:** iOS (iPhone + iPad, minimum iOS 17)
* **Language:** Swift 6 with strict concurrency (`SWIFT_STRICT_CONCURRENCY=complete`)
* **Frameworks:** SwiftUI for all UI; SwiftData for persistence; AVFoundation (`AVAudioEngine`) for playback; Combine only for service-event plumbing inside view models
* **Architecture:** MVVM. Views bind to view models; view models own services (`AudioPlayerService`, `VagdhenuClient`, `MantraRepository`); models are SwiftData types. No business logic in views.
* **Project generation:** XcodeGen (`project.yml`). Do not hand-edit the `.xcodeproj`.
* **Testing:** Swift Testing (`import Testing`, `@Test`) for unit tests in `Mantra/Tests/`; XCUITest for UI flows. New features require tests.
* **TTS integration:** Hybrid per `docs/architecture.md` — bundled corpus (offline) + Modal GPU worker for custom verses. Contract in `docs/api-contract.md`; the Swift client and Python worker must not drift.
* **Configuration:** The TTS endpoint is a user setting (UserDefaults); the API key is a credential stored in the **Keychain** (`Mantra/Services/KeychainStore.swift`), never hard-coded and never in UserDefaults.

# Freebuff Operational Rules
* **Planning First:** For any task involving multiple files or structural changes, propose a step-by-step plan before writing code.
* **Non-Destructive:** Do not delete or overwrite existing configurations or core files without explicitly asking for confirmation.
* **Complete Code:** Provide complete, working code segments. Avoid leaving placeholders like `// TODO: implement later` unless instructed.
