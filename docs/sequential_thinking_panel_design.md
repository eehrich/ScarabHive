# Sequential Thinking Panel - Design Document

**Version**: 1.0  
**Date**: 2025-10-27  
**Status**: Design Phase  
**Author**: Agent System Team

---

## Executive Summary

This document proposes a real-time visualization panel for the Sequential Thinking plugin, enabling users to observe and interact with the LLM's reasoning process. The panel provides:

- **Live thought stream** with real-time updates
- **Branch visualization** as interactive tree graph
- **Progress tracking** with thought timeline
- **Session management** with multi-session support
- **Inspection tools** for detailed thought analysis

**Key Benefits**:
- Transparent AI reasoning process
- Early detection of reasoning errors
- Interactive exploration of alternative approaches
- Educational insight into LLM problem-solving

---

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Requirements](#2-requirements)
3. [Architecture](#3-architecture)
4. [UI/UX Design](#4-uiux-design)
5. [Technical Implementation](#5-technical-implementation)
6. [Data Flow](#6-data-flow)
7. [API Specification](#7-api-specification)
8. [State Management](#8-state-management)
9. [Visualization Components](#9-visualization-components)
10. [Real-Time Updates](#10-real-time-updates)
11. [Performance Considerations](#11-performance-considerations)
12. [Security & Privacy](#12-security--privacy)
13. [Future Enhancements](#13-future-enhancements)
14. [Implementation Roadmap](#14-implementation-roadmap)

---

## 1. Problem Statement

### Current Situation

Users interact with sequential thinking through:
- Tool calls in chat interface
- Text-based summaries (`get_summary`)
- System prompt injections (when hook enabled)

**Limitations**:
- ❌ No visual overview of reasoning structure
- ❌ Can't see thought progression in real-time
- ❌ Branch relationships unclear in text format
- ❌ No way to jump to specific thoughts
- ❌ Multiple sessions hard to compare
- ❌ Historical sessions invisible after completion

### Desired Outcome

A dedicated panel that:
- ✅ Shows live reasoning as it happens
- ✅ Visualizes branch structure as graph
- ✅ Allows inspection of any thought
- ✅ Supports session comparison
- ✅ Preserves reasoning history
- ✅ Enables interactive exploration

---

## 2. Requirements

### 2.1 Functional Requirements

**FR1: Real-Time Thought Display**
- Display thoughts as they're created (SSE stream)
- Show thought content, number, timestamp
- Indicate branch, revision status
- Auto-scroll to latest thought

**FR2: Branch Visualization**
- Interactive tree graph of branches
- Visual distinction between active/inactive branches
- Click branch to view thoughts
- Highlight current branch

**FR3: Session Management**
- List all active sessions
- Switch between sessions
- Create new session
- Clear/archive sessions
- Compare multiple sessions side-by-side

**FR4: Thought Inspection**
- Click thought to view full content
- Show metadata (timestamp, branch, revisions)
- Display revision history
- Navigate to related thoughts

**FR5: Progress Tracking**
- Visual progress bar (current/estimate)
- Timeline view of thoughts
- Estimate adjustment indicators
- Completion status

**FR6: Search & Filter**
- Search thoughts by content
- Filter by branch
- Filter by time range
- Highlight search matches

### 2.2 Non-Functional Requirements

**NFR1: Performance**
- Handle 100+ thoughts without lag
- Render updates within 100ms
- Support 5+ concurrent sessions
- Efficient graph rendering

**NFR2: Usability**
- Intuitive navigation
- Responsive design (desktop + tablet)
- Keyboard shortcuts
- Accessible (WCAG 2.1 AA)

**NFR3: Reliability**
- Reconnect on connection loss
- Preserve state on refresh
- Handle corrupted data gracefully
- Error recovery

**NFR4: Compatibility**
- Modern browsers (Chrome 90+, Firefox 88+, Safari 14+)
- Integrate with existing WebUI
- Work with authentication system
- Support dark/light themes

---

## 3. Architecture

### 3.1 System Components

```
┌─────────────────────────────────────────────────────────┐
│                      Browser (Client)                    │
├─────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │   Chat UI    │  │ Thinking Panel│  │ Graph Canvas │  │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  │
│         │                  │                  │          │
│         └─────────┬────────┴────────┬─────────┘          │
│                   │                  │                    │
│           ┌───────▼──────────────────▼───────┐           │
│           │   WebSocket/SSE Manager          │           │
│           └───────┬──────────────────────────┘           │
└───────────────────┼──────────────────────────────────────┘
                    │
        ┌───────────▼───────────┐
        │   API Gateway (FastAPI)│
        └───────────┬───────────┘
                    │
        ┌───────────▼───────────┐
        │ Sequential Thinking    │
        │   Plugin (Server)      │
        └───────────┬───────────┘
                    │
        ┌───────────▼───────────┐
        │   Session Storage      │
        │   (In-Memory + Redis)  │
        └────────────────────────┘
```

### 3.2 Component Responsibilities

**Chat UI**
- Primary interaction point
- Triggers sequential thinking tool
- Shows thought summaries inline

**Thinking Panel**
- Dedicated panel (sidebar or modal)
- Displays thought stream
- Session switcher
- Thought inspector

**Graph Canvas**
- SVG/Canvas-based branch visualization
- Interactive node selection
- Zoom/pan controls
- Layout algorithm (tree/force-directed)

**WebSocket/SSE Manager**
- Real-time event stream
- Connection management
- Reconnection logic
- Event buffering

**API Gateway**
- REST endpoints for CRUD operations
- SSE endpoint for live updates
- Authentication/authorization
- Rate limiting

**Sequential Thinking Plugin**
- Core reasoning logic (existing)
- Event emission on state changes
- Session queries
- Thought storage

---

## 4. UI/UX Design

### 4.1 Layout Options

**Option A: Sidebar Panel** (Recommended)
```
┌────────────────────────────────────────────────────────┐
│  Agent System                         [Settings] [User] │
├──────────────────┬─────────────────────────────────────┤
│                  │                                      │
│                  │  Chat Messages                       │
│  Sequential      │  ┌────────────────────────────────┐ │
│  Thinking        │  │ User: Analyze this problem     │ │
│  Panel           │  └────────────────────────────────┘ │
│                  │  ┌────────────────────────────────┐ │
│  [Sessions ▼]    │  │ Agent: Using sequential        │ │
│  • Main (active) │  │ thinking...                    │ │
│  • Alt Approach  │  └────────────────────────────────┘ │
│                  │                                      │
│  [Branch Tree]   │  [Message Input]                    │
│  ┌──────────┐    │                                      │
│  │  Graph   │    │                                      │
│  │  View    │    │                                      │
│  └──────────┘    │                                      │
│                  │                                      │
│  [Thoughts]      │                                      │
│  #1: Analysis... │                                      │
│  #2: Hypothesis..│                                      │
│  #3: Testing...  │                                      │
│                  │                                      │
└──────────────────┴─────────────────────────────────────┘
```

**Option B: Bottom Panel**
```
┌────────────────────────────────────────────────────────┐
│  Agent System                         [Settings] [User] │
├────────────────────────────────────────────────────────┤
│  Chat Messages                                          │
│  ┌────────────────────────────────────────────────────┐│
│  │ Conversation here...                               ││
│  └────────────────────────────────────────────────────┘│
├────────────────────────────────────────────────────────┤
│  Sequential Thinking: Main Session    [Collapse ▲]     │
├──────────────┬─────────────────────────────────────────┤
│  Branch Tree │  Thought Stream                         │
│              │  #1: Analysis... (2m ago)               │
│              │  #2: Hypothesis... (1m ago)             │
│              │  #3 [alternative]: Testing... (30s ago) │
└──────────────┴─────────────────────────────────────────┘
```

**Option C: Modal Overlay**
- Full-screen modal
- Triggered by button in chat
- Focus mode for deep analysis
- Less suitable for live monitoring

### 4.2 Detailed Panel Layout (Sidebar)

```
┌─────────────────────────────────────────────┐
│ Sequential Thinking              [Pin] [X]  │
├─────────────────────────────────────────────┤
│                                             │
│ Session: Main ▼                [New] [⚙️]   │
│                                             │
├─────────────────────────────────────────────┤
│                                             │
│        Branch Visualization                 │
│                                             │
│         ┌─── main ───┐                      │
│         │            │                      │
│    ┌────#1─┐    ┌───#2───┐                 │
│    │  ...  │    │   ...  │                 │
│    └───────┘    └────┬───┘                 │
│                      │                      │
│                 ┌────#3────┐                │
│                 │          │                │
│            ┌────#4    #4[alt]───┐           │
│            │                    │           │
│         ┌──#5──┐            ┌──#5──┐       │
│         │ ...  │            │ ...  │       │
│         └──────┘            └──────┘       │
│                                             │
│  [Zoom: 100%] [Fit] [Reset]                │
│                                             │
├─────────────────────────────────────────────┤
│                                             │
│ Progress: 5/9 thoughts ██████░░░ 56%        │
│ Est. updated 2 times                        │
│                                             │
├─────────────────────────────────────────────┤
│                                             │
│ 🔍 Search thoughts...                       │
│ [All Branches ▼] [Time: All ▼]             │
│                                             │
├─────────────────────────────────────────────┤
│ Thoughts                         [Expand]   │
│                                             │
│ ┌─────────────────────────────────────────┐│
│ │ #1 [main] 2m ago                        ││
│ │ First, let's break down the problem    ││
│ │ into key components...                 ││
│ │ [View Details]                          ││
│ ├─────────────────────────────────────────┤│
│ │ #2 [main] 1m ago                        ││
│ │ Now examining the API design...        ││
│ │ [View Details]                          ││
│ ├─────────────────────────────────────────┤│
│ │ #3 [alternative] 30s ago                ││
│ │ Alternative: What if we use GraphQL... ││
│ │ [View Details] [Switch to main]        ││
│ ├─────────────────────────────────────────┤│
│ │ #4 [alternative] 5s ago ← ACTIVE       ││
│ │ Testing GraphQL approach...            ││
│ │ [View Details]                          ││
│ └─────────────────────────────────────────┘│
│                                             │
│ [Clear History] [Export JSON]              │
│                                             │
└─────────────────────────────────────────────┘
```

### 4.3 Thought Detail View (Modal)

```
┌─────────────────────────────────────────────────────────┐
│ Thought #3 [alternative branch]              [Close X] │
├─────────────────────────────────────────────────────────┤
│                                                         │
│ Created: 30 seconds ago (2025-10-27 22:45:12)          │
│ Branch: alternative (branched from #2)                 │
│ Status: Active | Revisions: None                       │
│                                                         │
├─────────────────────────────────────────────────────────┤
│                                                         │
│ Content:                                                │
│                                                         │
│ Alternative approach: What if we use GraphQL instead   │
│ of REST? This could give us:                           │
│                                                         │
│ 1. More flexible queries                               │
│ 2. Single endpoint                                     │
│ 3. Better type safety                                  │
│                                                         │
│ However, concerns:                                     │
│ - Learning curve for team                              │
│ - Caching complexity                                   │
│ - Potential over-fetching                              │
│                                                         │
├─────────────────────────────────────────────────────────┤
│                                                         │
│ Metadata:                                               │
│ • Thought Number: 3 (server), 3 (client)               │
│ • Branch ID: alternative                               │
│ • Parent Thought: #2                                   │
│ • Child Thoughts: #4                                   │
│ • Revision Count: 0                                    │
│                                                         │
├─────────────────────────────────────────────────────────┤
│                                                         │
│ Actions:                                                │
│ [Switch to Branch] [Revise Thought] [Create Branch]   │
│ [Copy Content] [View Context]                          │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### 4.4 Visual Design Elements

**Color Scheme** (Dark Mode):
```
Background:     #1e1e1e
Panel BG:       #252526
Border:         #3e3e42
Text Primary:   #cccccc
Text Secondary: #969696
Accent:         #007acc
Success:        #4ec9b0
Warning:        #ce9178
Error:          #f48771

Branches:
- main:         #4ec9b0 (green)
- alternative:  #dcdcaa (yellow)
- archived:     #6a737d (gray)
```

**Typography**:
```
Heading:        -apple-system, BlinkMacSystemFont, "Segoe UI"
Body:           -apple-system, BlinkMacSystemFont, "Segoe UI"
Code:           "Fira Code", "Consolas", monospace

Sizes:
H1: 18px / 1.5 / 600
H2: 16px / 1.4 / 600
H3: 14px / 1.3 / 600
Body: 13px / 1.5 / 400
Small: 11px / 1.4 / 400
```

**Icons** (Using Lucide or similar):
- Session: 🧠 `brain`
- Branch: 🌿 `git-branch`
- Thought: 💭 `message-circle`
- Revision: 🔄 `refresh-cw`
- Active: ⚡ `zap`
- Archived: 📦 `archive`
- Search: 🔍 `search`
- Export: 📥 `download`

### 4.5 Interactions

**Branch Graph**:
- **Hover node**: Show tooltip with thought summary
- **Click node**: Select thought, highlight in list
- **Double-click node**: Open thought detail modal
- **Click branch line**: Highlight all thoughts in branch
- **Drag**: Pan graph
- **Scroll**: Zoom in/out
- **Click background**: Deselect

**Thought List**:
- **Click thought**: Expand/collapse full content
- **Hover**: Show quick actions (revise, branch, view)
- **Right-click**: Context menu (copy, delete, etc.)
- **Drag**: Reorder (if manual sorting enabled)

**Keyboard Shortcuts**:
```
Ctrl+K          : Focus search
Ctrl+N          : New session
Ctrl+W          : Close panel
Ctrl+E          : Export session
Ctrl+F          : Toggle fullscreen
Arrow Up/Down   : Navigate thoughts
Enter           : Open selected thought
Esc             : Close modal/deselect
```

---

## 5. Technical Implementation

### 5.1 Technology Stack

**Frontend**:
- **Framework**: React 18+ (hooks, suspense)
- **State**: Zustand or Jotai (lightweight)
- **Graph Viz**: D3.js or Cytoscape.js
- **UI Components**: Radix UI (headless, accessible)
- **Styling**: Tailwind CSS + CSS Modules
- **Real-Time**: EventSource (SSE) or Socket.io
- **Charts**: Recharts (for timeline/progress)
- **Animations**: Framer Motion

**Backend** (additions to existing):
- **Events**: Python `asyncio` events
- **SSE**: FastAPI StreamingResponse
- **Caching**: Redis (for session snapshots)
- **Serialization**: Pydantic models → JSON

**Build Tools**:
- **Bundler**: Vite (fast dev server)
- **TypeScript**: Type safety
- **Linter**: ESLint + Prettier
- **Testing**: Vitest + React Testing Library

### 5.2 File Structure

```
src/
├── agent_system/
│   ├── app.py                      # Add SSE endpoints
│   └── ...
├── plugins/
│   └── sequential_thinking/
│       ├── server.py               # Add event emitters
│       ├── events.py               # NEW: Event definitions
│       └── ...
└── ...

static/
├── js/
│   ├── sequential_thinking/        # NEW: Panel module
│   │   ├── index.js                # Entry point
│   │   ├── components/
│   │   │   ├── Panel.jsx           # Main panel container
│   │   │   ├── SessionSelector.jsx
│   │   │   ├── BranchGraph.jsx     # Graph visualization
│   │   │   ├── ThoughtList.jsx
│   │   │   ├── ThoughtCard.jsx
│   │   │   ├── ThoughtDetail.jsx   # Modal
│   │   │   ├── ProgressBar.jsx
│   │   │   └── SearchFilter.jsx
│   │   ├── hooks/
│   │   │   ├── useThinkingSession.js
│   │   │   ├── useSSE.js
│   │   │   ├── useGraph.js
│   │   │   └── useThoughts.js
│   │   ├── stores/
│   │   │   ├── sessionStore.js     # Zustand store
│   │   │   └── uiStore.js
│   │   ├── utils/
│   │   │   ├── graphLayout.js
│   │   │   ├── formatters.js
│   │   │   └── api.js
│   │   └── types/
│   │       └── index.ts            # TypeScript types
│   └── ...
└── css/
    └── sequential_thinking.css

templates/
└── index.html                      # Add panel placeholder
```

### 5.3 Component Hierarchy

```
<ThinkingPanel>
  ├── <PanelHeader>
  │   ├── <SessionSelector>
  │   └── <PanelControls>
  │
  ├── <BranchGraph>
  │   └── <GraphCanvas>
  │       ├── <Node> (multiple)
  │       ├── <Edge> (multiple)
  │       └── <GraphControls>
  │
  ├── <ProgressSection>
  │   ├── <ProgressBar>
  │   └── <EstimateIndicator>
  │
  ├── <SearchFilter>
  │   ├── <SearchInput>
  │   └── <FilterDropdowns>
  │
  ├── <ThoughtList>
  │   └── <ThoughtCard> (multiple)
  │       ├── <ThoughtHeader>
  │       ├── <ThoughtContent>
  │       └── <ThoughtActions>
  │
  ├── <PanelFooter>
  │   └── <ActionButtons>
  │
  └── <ThoughtDetailModal> (conditional)
      ├── <ModalHeader>
      ├── <ThoughtContent>
      ├── <MetadataSection>
      └── <ActionBar>
```

---

## 6. Data Flow

### 6.1 Initial Load

```
User Opens Chat
     ↓
Panel Mounts
     ↓
Fetch Active Sessions (REST: GET /api/thinking/sessions)
     ↓
Display Session List
     ↓
User Selects Session (or auto-select most recent)
     ↓
Fetch Session Details (REST: GET /api/thinking/sessions/{id})
     ↓
Subscribe to Updates (SSE: /api/thinking/sessions/{id}/stream)
     ↓
Render Graph + Thoughts
```

### 6.2 Real-Time Updates

```
LLM Calls sequential_thinking()
     ↓
Plugin Creates Thought
     ↓
Emit Event: thought_created
     ↓
SSE Stream Sends Event to Connected Clients
     ↓
Panel Receives Event
     ↓
Update Store (add thought to list)
     ↓
React Re-renders
     ↓
- Append to ThoughtList
- Update BranchGraph (add node)
- Update ProgressBar
     ↓
Animate New Thought (fade in)
```

### 6.3 User Interaction Flow

**Example: Switch Branch**

```
User Clicks "Switch to alternative"
     ↓
POST /api/thinking/sessions/{id}/switch-branch
Body: { branch_id: "alternative" }
     ↓
Server Updates Session State
     ↓
Emit Event: branch_switched
     ↓
SSE Streams Event
     ↓
Panel Updates:
- Highlight new active branch in graph
- Update current_branch indicator
- Filter thoughts (optional)
     ↓
LLM continues with new branch context
```

---

## 7. API Specification

### 7.1 REST Endpoints

**GET /api/thinking/sessions**
```json
Response 200:
{
  "sessions": [
    {
      "session_id": "abc123",
      "created_at": "2025-10-27T22:00:00Z",
      "last_accessed": "2025-10-27T22:30:00Z",
      "total_thoughts": 5,
      "current_branch": "main",
      "branches": ["main", "alternative"],
      "estimate": 9,
      "status": "active"
    }
  ]
}
```

**GET /api/thinking/sessions/{session_id}**
```json
Response 200:
{
  "session_id": "abc123",
  "created_at": "2025-10-27T22:00:00Z",
  "last_accessed": "2025-10-27T22:30:00Z",
  "current_branch": "main",
  "total_thoughts": 5,
  "estimate": 9,
  "branches": {
    "main": {
      "parent": null,
      "branched_from": null,
      "thoughts_count": 4,
      "active": true
    },
    "alternative": {
      "parent": "main",
      "branched_from": 2,
      "thoughts_count": 1,
      "active": false
    }
  },
  "thoughts": [
    {
      "number": 1,
      "content": "First, let's analyze...",
      "timestamp": "2025-10-27T22:00:00Z",
      "branch_id": "main",
      "is_revision": false,
      "revises_thought": null,
      "revision_history": []
    }
  ]
}
```

**POST /api/thinking/sessions/{session_id}/switch-branch**
```json
Request:
{
  "branch_id": "alternative"
}

Response 200:
{
  "status": "success",
  "session_id": "abc123",
  "current_branch": "alternative"
}
```

**DELETE /api/thinking/sessions/{session_id}**
```json
Response 200:
{
  "status": "success",
  "message": "Session cleared"
}
```

**POST /api/thinking/sessions**
```json
Request:
{
  "agent_session_id": "xyz789"  // Optional
}

Response 201:
{
  "session_id": "new123",
  "created_at": "2025-10-27T22:35:00Z"
}
```

### 7.2 SSE Events

**Stream Endpoint**: `GET /api/thinking/sessions/{session_id}/stream`

**Event Types**:

**thought_created**
```json
{
  "event": "thought_created",
  "data": {
    "session_id": "abc123",
    "thought": {
      "number": 6,
      "content": "New insight...",
      "timestamp": "2025-10-27T22:35:00Z",
      "branch_id": "main",
      "is_revision": false
    }
  }
}
```

**branch_created**
```json
{
  "event": "branch_created",
  "data": {
    "session_id": "abc123",
    "branch_id": "alternative",
    "parent_branch": "main",
    "branched_from_thought": 2
  }
}
```

**branch_switched**
```json
{
  "event": "branch_switched",
  "data": {
    "session_id": "abc123",
    "from_branch": "main",
    "to_branch": "alternative"
  }
}
```

**estimate_updated**
```json
{
  "event": "estimate_updated",
  "data": {
    "session_id": "abc123",
    "old_estimate": 9,
    "new_estimate": 12
  }
}
```

**thought_revised**
```json
{
  "event": "thought_revised",
  "data": {
    "session_id": "abc123",
    "thought_number": 3,
    "revision": {
      "content": "Revised thinking...",
      "timestamp": "2025-10-27T22:36:00Z"
    }
  }
}
```

**session_completed**
```json
{
  "event": "session_completed",
  "data": {
    "session_id": "abc123",
    "total_thoughts": 10,
    "duration_seconds": 120
  }
}
```

---

## 8. State Management

### 8.1 Zustand Store Structure

```typescript
interface ThinkingStore {
  // Sessions
  sessions: Map<string, Session>;
  activeSessionId: string | null;
  
  // UI State
  panelOpen: boolean;
  selectedThoughtId: number | null;
  detailModalOpen: boolean;
  searchQuery: string;
  filterBranch: string | null;
  filterTimeRange: TimeRange | null;
  
  // Graph State
  graphZoom: number;
  graphPan: { x: number; y: number };
  selectedNodeId: string | null;
  
  // Actions
  setActiveSession: (id: string) => void;
  addThought: (sessionId: string, thought: Thought) => void;
  updateThought: (sessionId: string, thoughtId: number, updates: Partial<Thought>) => void;
  createBranch: (sessionId: string, branch: Branch) => void;
  switchBranch: (sessionId: string, branchId: string) => void;
  setSearchQuery: (query: string) => void;
  setFilterBranch: (branchId: string | null) => void;
  selectThought: (thoughtId: number | null) => void;
  togglePanel: () => void;
  openDetailModal: (thoughtId: number) => void;
  closeDetailModal: () => void;
}
```

### 8.2 Data Models

```typescript
interface Session {
  session_id: string;
  created_at: string;
  last_accessed: string;
  current_branch: string;
  total_thoughts: number;
  estimate: number;
  branches: Map<string, Branch>;
  thoughts: Thought[];
  status: 'active' | 'completed' | 'archived';
}

interface Branch {
  branch_id: string;
  parent_branch: string | null;
  branched_from_thought: number | null;
  thoughts_count: number;
  active: boolean;
  created_at: string;
}

interface Thought {
  number: number;
  content: string;
  timestamp: string;
  branch_id: string;
  is_revision: boolean;
  revises_thought: number | null;
  revision_history: Revision[];
  metadata?: Record<string, any>;
}

interface Revision {
  timestamp: string;
  content: string;
  branch_id: string;
}
```

---

## 9. Visualization Components

### 9.1 Branch Graph Implementation

**Library**: D3.js (force-directed layout) or Cytoscape.js

**Graph Layout Algorithm**:

```javascript
// Hierarchical tree layout
function calculateBranchLayout(branches, thoughts) {
  const nodes = [];
  const edges = [];
  
  // Create nodes for each thought
  thoughts.forEach(thought => {
    nodes.push({
      id: `thought-${thought.number}`,
      label: `#${thought.number}`,
      branch: thought.branch_id,
      data: thought,
      level: calculateDepth(thought, branches)
    });
  });
  
  // Create edges between sequential thoughts
  thoughts.forEach((thought, i) => {
    if (i > 0) {
      const prev = thoughts[i - 1];
      if (prev.branch_id === thought.branch_id) {
        edges.push({
          source: `thought-${prev.number}`,
          target: `thought-${thought.number}`,
          type: 'sequence'
        });
      }
    }
  });
  
  // Create edges for branches
  branches.forEach(branch => {
    if (branch.branched_from_thought) {
      const branchThoughts = thoughts.filter(t => t.branch_id === branch.branch_id);
      if (branchThoughts.length > 0) {
        edges.push({
          source: `thought-${branch.branched_from_thought}`,
          target: `thought-${branchThoughts[0].number}`,
          type: 'branch'
        });
      }
    }
  });
  
  return { nodes, edges };
}

// D3 force simulation
function initializeGraph(nodes, edges) {
  const simulation = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(edges).id(d => d.id).distance(100))
    .force('charge', d3.forceManyBody().strength(-300))
    .force('x', d3.forceX(width / 2).strength(0.1))
    .force('y', d3.forceY(d => d.level * 80).strength(0.5))
    .force('collision', d3.forceCollide().radius(30));
  
  return simulation;
}
```

**Node Rendering**:

```jsx
function ThoughtNode({ thought, isActive, isSelected, onClick }) {
  return (
    <g 
      className="thought-node"
      transform={`translate(${thought.x}, ${thought.y})`}
      onClick={() => onClick(thought)}
    >
      {/* Circle background */}
      <circle
        r={20}
        className={cn(
          'node-circle',
          isActive && 'active',
          isSelected && 'selected'
        )}
        fill={getBranchColor(thought.branch_id)}
        stroke={isSelected ? '#007acc' : '#3e3e42'}
        strokeWidth={isSelected ? 3 : 1}
      />
      
      {/* Thought number */}
      <text
        textAnchor="middle"
        dy="0.3em"
        fill="#fff"
        fontSize={12}
        fontWeight={600}
      >
        {thought.number}
      </text>
      
      {/* Revision indicator */}
      {thought.is_revision && (
        <circle
          cx={15}
          cy={-15}
          r={5}
          fill="#ce9178"
        />
      )}
      
      {/* Active indicator */}
      {isActive && (
        <circle
          cx={0}
          cy={0}
          r={25}
          fill="none"
          stroke="#4ec9b0"
          strokeWidth={2}
          className="active-pulse"
        />
      )}
    </g>
  );
}
```

**Edge Rendering**:

```jsx
function ThoughtEdge({ edge, type }) {
  const path = type === 'branch' 
    ? `M ${edge.source.x} ${edge.source.y} 
       Q ${(edge.source.x + edge.target.x) / 2} ${edge.source.y}
       ${edge.target.x} ${edge.target.y}`
    : `M ${edge.source.x} ${edge.source.y} 
       L ${edge.target.x} ${edge.target.y}`;
  
  return (
    <path
      d={path}
      stroke={type === 'branch' ? '#dcdcaa' : '#3e3e42'}
      strokeWidth={2}
      strokeDasharray={type === 'branch' ? '5,5' : 'none'}
      fill="none"
      markerEnd="url(#arrowhead)"
    />
  );
}
```

### 9.2 Timeline Visualization

Alternative to graph for linear view:

```jsx
function ThoughtTimeline({ thoughts }) {
  return (
    <div className="timeline">
      {thoughts.map((thought, i) => (
        <div key={thought.number} className="timeline-item">
          <div className="timeline-marker">
            <div 
              className="marker-dot"
              style={{ backgroundColor: getBranchColor(thought.branch_id) }}
            />
            {i < thoughts.length - 1 && (
              <div className="marker-line" />
            )}
          </div>
          
          <div className="timeline-content">
            <div className="timeline-header">
              <span className="thought-number">#{thought.number}</span>
              <span className="thought-branch">[{thought.branch_id}]</span>
              <span className="thought-time">{formatRelativeTime(thought.timestamp)}</span>
            </div>
            <div className="timeline-text">
              {truncate(thought.content, 100)}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
```

---

## 10. Real-Time Updates

### 10.1 SSE Connection Management

```typescript
class ThinkingSSEClient {
  private eventSource: EventSource | null = null;
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 5;
  private reconnectDelay = 1000;
  
  connect(sessionId: string, onEvent: (event: ThinkingEvent) => void) {
    const url = `/api/thinking/sessions/${sessionId}/stream`;
    const token = localStorage.getItem('auth_token');
    
    this.eventSource = new EventSource(`${url}?token=${token}`);
    
    this.eventSource.onmessage = (event) => {
      const data = JSON.parse(event.data);
      onEvent(data);
      this.reconnectAttempts = 0; // Reset on successful message
    };
    
    this.eventSource.onerror = (error) => {
      console.error('SSE error:', error);
      this.handleReconnect(sessionId, onEvent);
    };
  }
  
  private handleReconnect(sessionId: string, onEvent: (event: ThinkingEvent) => void) {
    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
      console.error('Max reconnect attempts reached');
      return;
    }
    
    this.disconnect();
    this.reconnectAttempts++;
    
    const delay = this.reconnectDelay * Math.pow(2, this.reconnectAttempts);
    
    setTimeout(() => {
      console.log(`Reconnecting... attempt ${this.reconnectAttempts}`);
      this.connect(sessionId, onEvent);
    }, delay);
  }
  
  disconnect() {
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
    }
  }
}
```

### 10.2 React Hook for SSE

```typescript
function useThinkingStream(sessionId: string | null) {
  const [client] = useState(() => new ThinkingSSEClient());
  const updateStore = useThinkingStore(state => state.addThought);
  
  useEffect(() => {
    if (!sessionId) return;
    
    client.connect(sessionId, (event) => {
      switch (event.event) {
        case 'thought_created':
          updateStore(sessionId, event.data.thought);
          break;
        case 'branch_created':
          // Handle branch creation
          break;
        // ... other events
      }
    });
    
    return () => client.disconnect();
  }, [sessionId]);
}
```

---

## 11. Performance Considerations

### 11.1 Optimization Strategies

**Virtual Scrolling** (for large thought lists):
```jsx
import { VirtualList } from 'react-virtual';

function ThoughtList({ thoughts }) {
  const parentRef = useRef();
  
  const rowVirtualizer = useVirtualizer({
    count: thoughts.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 80,
    overscan: 5
  });
  
  return (
    <div ref={parentRef} className="thought-list-container">
      <div style={{ height: `${rowVirtualizer.getTotalSize()}px` }}>
        {rowVirtualizer.getVirtualItems().map(virtualRow => (
          <ThoughtCard
            key={thoughts[virtualRow.index].number}
            thought={thoughts[virtualRow.index]}
            style={{
              position: 'absolute',
              top: 0,
              left: 0,
              transform: `translateY(${virtualRow.start}px)`
            }}
          />
        ))}
      </div>
    </div>
  );
}
```

**Debounced Search**:
```typescript
const debouncedSearch = useMemo(
  () => debounce((query: string) => {
    setSearchQuery(query);
  }, 300),
  []
);
```

**Memoized Graph Layout**:
```typescript
const graphLayout = useMemo(() => {
  return calculateBranchLayout(branches, thoughts);
}, [branches.length, thoughts.length]); // Only recalc when counts change
```

**Lazy Loading**:
- Load initial 50 thoughts
- Load more on scroll
- Paginate session list

### 11.2 Rendering Performance

**React.memo for expensive components**:
```typescript
export const ThoughtCard = React.memo(
  ({ thought, isSelected, onClick }) => {
    // Component implementation
  },
  (prevProps, nextProps) => {
    return (
      prevProps.thought.number === nextProps.thought.number &&
      prevProps.isSelected === nextProps.isSelected
    );
  }
);
```

**Canvas fallback for large graphs**:
- Use SVG for < 100 nodes
- Switch to Canvas for >= 100 nodes
- WebGL for > 500 nodes (via PixiJS)

---

## 12. Security & Privacy

### 12.1 Authentication

- Reuse existing JWT authentication
- Include token in SSE query parameter (EventSource limitation)
- Validate token server-side for each SSE connection
- Implement connection rate limiting

### 12.2 Authorization

- Users can only view their own sessions
- Admin users can view all sessions (optional)
- Session ownership verified on every API call

### 12.3 Data Privacy

- Thought content may contain sensitive information
- No external analytics on thought data
- Optional: Encrypt thought content at rest
- Clear separation between users' sessions

### 12.4 XSS Prevention

- Sanitize thought content before rendering
- Use DOMPurify for HTML sanitization
- Escape user input in search queries
- CSP headers to prevent inline scripts

---

## 13. Future Enhancements

### Phase 2 (Post-MVP)

**13.1 Collaborative Features**
- Share session with team members
- Real-time multi-user observation
- Commenting on thoughts
- Annotation tools

**13.2 Advanced Visualizations**
- Heatmap of thought complexity
- Sentiment analysis visualization
- Confidence scoring per thought
- Topic clustering

**13.3 Export & Analysis**
- Export as PDF/Markdown
- Jupyter notebook export
- Statistical analysis dashboard
- Thought pattern recognition

**13.4 AI-Assisted Features**
- Suggest missing branches
- Detect circular reasoning
- Highlight contradictions
- Auto-generate summary

**13.5 Integration**
- VSCode extension
- Slack notifications
- Webhook triggers
- API for third-party tools

### Phase 3 (Future)

**13.6 Advanced Graph Features**
- Multiple graph layouts (radial, hierarchical, force)
- Graph comparison (diff two sessions)
- Merge branches
- Thought dependency tracking

**13.7 Historical Analysis**
- Session replay (watch reasoning unfold)
- Pattern analysis across sessions
- Success rate tracking
- Learning from past reasoning

**13.8 Mobile Support**
- Progressive Web App (PWA)
- Touch-optimized interactions
- Offline mode
- Native mobile apps

---

## 14. Implementation Roadmap

### MVP (Minimum Viable Product) - 4 weeks

**Week 1: Foundation**
- [ ] API endpoints (REST + SSE)
- [ ] Event emission in plugin
- [ ] Basic data models
- [ ] Authentication integration

**Week 2: Core UI**
- [ ] Panel layout (sidebar)
- [ ] Session selector
- [ ] Thought list with virtual scrolling
- [ ] Basic styling

**Week 3: Graph Visualization**
- [ ] D3.js graph implementation
- [ ] Node/edge rendering
- [ ] Interactive selection
- [ ] Zoom/pan controls

**Week 4: Polish & Testing**
- [ ] Real-time updates
- [ ] Search & filter
- [ ] Thought detail modal
- [ ] Error handling
- [ ] Unit tests
- [ ] Integration tests
- [ ] Documentation

### Post-MVP - 2-4 weeks each

**Phase 1.5: Enhancements**
- [ ] Timeline view
- [ ] Progress visualization
- [ ] Keyboard shortcuts
- [ ] Export functionality
- [ ] Performance optimizations

**Phase 2: Advanced Features**
- [ ] Session comparison
- [ ] Branch merging
- [ ] Annotation tools
- [ ] Historical sessions

---

## Appendix A: Alternative Approaches

### A.1 Canvas vs SVG for Graphs

**SVG Pros**:
- DOM-based (easier interaction)
- Crisp at any zoom level
- Better for small-medium graphs (< 100 nodes)
- CSS styling

**SVG Cons**:
- Performance degrades with many nodes
- Memory intensive

**Canvas Pros**:
- Better performance (500+ nodes)
- Lower memory footprint
- Hardware-accelerated

**Canvas Cons**:
- Manual interaction handling
- Pixelation on zoom
- No CSS styling

**Recommendation**: SVG for MVP, Canvas fallback for large graphs

### A.2 State Management Options

**Zustand** (Recommended):
- Lightweight (~1KB)
- Simple API
- No boilerplate
- TypeScript support

**Redux Toolkit**:
- Industry standard
- DevTools integration
- More boilerplate
- Overkill for this use case

**Jotai**:
- Atomic state
- Very lightweight
- Less mature ecosystem

**Recommendation**: Zustand for simplicity and performance

---

## Appendix B: Accessibility Considerations

**Keyboard Navigation**:
- Tab through all interactive elements
- Arrow keys for graph navigation
- Enter/Space to activate
- Escape to close modals

**Screen Reader Support**:
- ARIA labels on all interactive elements
- Live regions for real-time updates
- Descriptive alt text for graph elements
- Semantic HTML structure

**Visual Accessibility**:
- WCAG AA contrast ratios
- No color-only information
- Scalable text
- Focus indicators
- Reduced motion option

---

## Appendix C: Error Scenarios

**Connection Lost**:
- Show "Reconnecting..." indicator
- Attempt reconnection (exponential backoff)
- Queue missed events (if possible)
- Graceful degradation

**Session Not Found**:
- Show error message
- Offer to create new session
- Redirect to session list

**Invalid Data**:
- Log error
- Show generic error to user
- Continue with partial data
- Attempt recovery

**Graph Rendering Failure**:
- Fallback to list view
- Log error details
- Offer refresh button

---

## Conclusion

This design provides a comprehensive foundation for implementing a Sequential Thinking visualization panel. The approach balances:

- **User needs**: Real-time visibility, intuitive navigation, detailed inspection
- **Technical feasibility**: Leverages existing architecture, proven technologies
- **Performance**: Optimized for 100+ thoughts, real-time updates
- **Extensibility**: Clean separation, hooks for future features

**Next Steps**:
1. ~~Review and approve design~~ ✅ Approved
2. ~~Create technical spike for graph visualization~~ → **Revised**: Use existing WebUI infrastructure
3. Implement using existing PanelManager + vanilla JS (see Appendix D)
4. Begin MVP implementation

**Success Metrics**:
- Panel loads in < 1s
- Real-time updates within 100ms
- Handles 200+ thoughts smoothly
- 90%+ user satisfaction (post-launch survey)

---

## Appendix D: Implementation with Existing WebUI Infrastructure

### D.1 Architecture Revision

**CRITICAL**: After initial design, project requirements clarified:
- ❌ **NO React/Zustand/modern frameworks** - avoid introducing new tech stack
- ✅ **USE existing WebUI patterns** - leverage PanelManager, plugin system, vanilla JS
- ✅ **Follow existing plugins** - TODO, context_usage_tracker, status_module patterns

### D.2 Existing Infrastructure Overview

#### Plugin UI Registration
Plugins register UI via `schema.yaml`:
```yaml
web_ui:
  button:
    enabled: true
    text: "🧠 Thinking"
    icon: "🧠"
    tooltip: "View sequential thinking visualization"
  
  panel:
    enabled: true
    title: "Sequential Thinking"
    endpoint: "/plugins/{{ name }}/panel"
    type: "iframe"  # Full custom UI (like TODO panel)
    width: "1200px"
    height: "800px"
```

#### Web Endpoints Pattern
Each plugin with UI creates `web_endpoints.py`:
```python
class ThinkingWebFactory:
    """Web UI factory for Sequential Thinking panel."""
    
    def __init__(self, server):
        self.server = server  # SequentialThinkingServer instance
        self.plugin_dir = Path(__file__).parent
        self.templates_dir = self.plugin_dir / "templates"
        self.templates = Jinja2Templates(directory=str(self.templates_dir))
    
    def get_web_router(self) -> APIRouter:
        router = APIRouter(prefix=f"/plugins/{self.server.name}")
        
        @router.get("/panel", response_class=HTMLResponse)
        async def get_panel(request: Request):
            """Render thinking visualization panel."""
            return self.templates.TemplateResponse(
                "panel.html",
                {"request": request, "plugin_name": self.server.name}
            )
        
        @router.get("/sessions")
        async def get_sessions():
            """Get all thinking sessions (JSON)."""
            return {"sessions": [...]}
        
        @router.get("/sessions/{session_id}/stream")
        async def stream_session(session_id: str):
            """SSE stream for real-time updates."""
            async def event_generator():
                while True:
                    event = await get_next_event(session_id)
                    yield f"data: {json.dumps(event)}\n\n"
            return StreamingResponse(event_generator(), media_type="text/event-stream")
        
        return router
```

#### Frontend Module Pattern
`static/js/thinking_panel_module.js` (follows `status_module.js` pattern):
```javascript
window.AgentSystem = window.AgentSystem || {};

window.AgentSystem.ThinkingPanel = (function() {
    'use strict';
    
    // State (plain objects, no Zustand)
    const state = {
        sessions: new Map(),
        currentSessionId: null,
        eventSource: null
    };
    
    // Initialize panel (called when button clicked)
    function showPanel() {
        const panelContent = `
            <div id="thinking-panel-container">
                <!-- Panel content will be injected here -->
            </div>
        `;
        
        // Use existing PanelManager
        window.AgentSystem.PanelManager.createPanel(
            'thinking-panel',
            '🧠 Sequential Thinking',
            panelContent,
            ['thinking-panel'],
            null  // No custom header
        );
        
        // Load initial data
        loadSessions();
        
        // Setup SSE for real-time updates
        setupEventStream();
    }
    
    // Load sessions via REST API
    async function loadSessions() {
        try {
            const response = await fetch('/plugins/sequential_thinking/sessions');
            const data = await response.json();
            state.sessions = new Map(data.sessions.map(s => [s.id, s]));
            renderSessionList();
        } catch (error) {
            console.error('Failed to load sessions:', error);
        }
    }
    
    // Setup EventSource for real-time updates
    function setupEventStream() {
        if (state.eventSource) {
            state.eventSource.close();
        }
        
        const sessionId = state.currentSessionId;
        if (!sessionId) return;
        
        // Follow chat_module.js pattern (lines 531-532)
        state.eventSource = new EventSource(`/plugins/sequential_thinking/sessions/${sessionId}/stream`);
        
        state.eventSource.addEventListener('thought_created', (event) => {
            const data = JSON.parse(event.data);
            handleThoughtCreated(data);
        });
        
        state.eventSource.addEventListener('branch_switched', (event) => {
            const data = JSON.parse(event.data);
            handleBranchSwitched(data);
        });
        
        state.eventSource.onerror = (error) => {
            console.error('EventSource error:', error);
            // Reconnection handled by browser
        };
    }
    
    // Render session list (plain JS, no React)
    function renderSessionList() {
        const container = document.getElementById('thinking-sessions-list');
        if (!container) return;
        
        let html = '<ul class="session-list">';
        state.sessions.forEach((session, id) => {
            html += `
                <li class="session-item ${id === state.currentSessionId ? 'active' : ''}"
                    onclick="AgentSystem.ThinkingPanel.selectSession('${id}')">
                    <span class="session-name">${session.name || 'Session ' + id.substr(0, 8)}</span>
                    <span class="session-meta">${session.thought_count} thoughts</span>
                </li>
            `;
        });
        html += '</ul>';
        container.innerHTML = html;
    }
    
    // Render thought graph (D3.js - acceptable to add for visualization)
    function renderThoughtGraph() {
        const container = document.getElementById('thinking-graph-container');
        if (!container) return;
        
        const session = state.sessions.get(state.currentSessionId);
        if (!session) return;
        
        // Use D3.js for graph rendering
        const width = container.clientWidth;
        const height = container.clientHeight;
        
        const svg = d3.select(container)
            .append('svg')
            .attr('width', width)
            .attr('height', height);
        
        // Tree layout for thought progression
        const treeLayout = d3.tree().size([width - 40, height - 40]);
        
        // Build hierarchy from thoughts
        const root = buildThoughtHierarchy(session.thoughts);
        const treeData = treeLayout(root);
        
        // Render links (connections)
        svg.selectAll('.link')
            .data(treeData.links())
            .enter()
            .append('path')
            .attr('class', 'link')
            .attr('d', d3.linkVertical()
                .x(d => d.x)
                .y(d => d.y)
            );
        
        // Render nodes (thoughts)
        const nodes = svg.selectAll('.node')
            .data(treeData.descendants())
            .enter()
            .append('g')
            .attr('class', 'node')
            .attr('transform', d => `translate(${d.x},${d.y})`)
            .on('click', (event, d) => selectThought(d.data));
        
        nodes.append('circle')
            .attr('r', 8)
            .attr('class', d => d.data.is_revision ? 'revision' : 'normal');
        
        nodes.append('text')
            .attr('dy', -12)
            .text(d => `#${d.data.server_thought_number}`);
    }
    
    // Public API
    return {
        showPanel,
        selectSession: (sessionId) => {
            state.currentSessionId = sessionId;
            setupEventStream();
            renderSessionList();
            renderThoughtGraph();
        },
        close: () => {
            if (state.eventSource) {
                state.eventSource.close();
            }
        }
    };
})();
```

### D.3 Implementation Plan

#### Phase 1: Backend (Server-Side)
1. **Create `web_endpoints.py`** in `src/plugins/sequential_thinking/`
   - Implement `ThinkingWebFactory` class
   - Add `/panel` endpoint (HTML template)
   - Add `/sessions` endpoint (list all sessions)
   - Add `/sessions/{id}` endpoint (session details)
   - Add `/sessions/{id}/stream` endpoint (SSE for real-time updates)

2. **Update `server.py`** to emit events
   - Emit `thought_created` event after `add_thought`
   - Emit `branch_switched` event after `switch_branch`
   - Emit `branch_created` event after creating new branch
   - Emit `session_created` event after new session
   - Use `asyncio.Queue` or in-memory pub/sub for event distribution

3. **Update `schema.yaml`** to register UI
   - Add `web_ui` section (button + panel config)
   - Configure as `type: "iframe"` for full custom UI

4. **Create template** `templates/panel.html`
   - Basic HTML structure (no React)
   - Include D3.js from CDN (for graph viz)
   - Container divs for sessions, graph, details
   - Reference `thinking_panel_module.js`

#### Phase 2: Frontend (Client-Side)
1. **Create `static/js/thinking_panel_module.js`**
   - Follow `status_module.js` pattern
   - Implement state management (plain objects/Maps)
   - Implement `showPanel()` using `PanelManager.createPanel()`
   - Implement `loadSessions()` (fetch from `/sessions`)
   - Implement `setupEventStream()` (EventSource pattern)
   - Implement `renderSessionList()` (plain JS DOM manipulation)
   - Implement `renderThoughtGraph()` (D3.js tree layout)
   - Implement event handlers (thought_created, branch_switched)

2. **Create `static/css/thinking_panel.css`**
   - Styles for session list
   - Styles for thought graph (nodes, links, labels)
   - Styles for thought details panel
   - Responsive layout (grid: sessions | graph | details)

3. **Update `templates/index.html`** (if needed)
   - Include `thinking_panel_module.js` script tag
   - Include `thinking_panel.css` link tag
   - (Or load dynamically when panel opened)

#### Phase 3: Integration
1. **Register web router** in plugin initialization
   - Call `web_factory.get_web_router()` in `__init__`
   - Mount router to FastAPI app

2. **Test plugin UI registration**
   - Verify button appears in plugin toolbar
   - Verify `/api/plugins/ui` returns thinking metadata
   - Verify clicking button opens panel

3. **Test real-time updates**
   - Create thought via MCP tool
   - Verify SSE event fires
   - Verify graph updates in real-time
   - Verify session list updates

#### Phase 4: Graph Visualization
1. **Implement D3.js tree layout**
   - Build hierarchy from thoughts (parent: revises_thought or previous thought)
   - Use `d3.tree()` for vertical layout
   - Render nodes as circles (color-coded: normal/revision/branch)
   - Render links as curved paths
   - Add labels (thought numbers, branch names)

2. **Add interactivity**
   - Click node to show thought details
   - Hover for quick preview
   - Zoom/pan support (`d3.zoom()`)
   - Highlight current thought
   - Highlight selected branch

3. **Add branch visualization**
   - Different colors for different branches
   - Branch labels at branch points
   - Branch metadata (creation time, description)

### D.4 Technology Stack (Revised)

| Component | Technology | Rationale |
|-----------|-----------|-----------|
| Panel System | `PanelManager.createPanel()` | Existing floating panel infrastructure |
| Module Pattern | `window.AgentSystem.ThinkingPanel` | Consistent with other modules |
| State Management | Plain JS objects + Map | No framework needed, simple state |
| Real-time Updates | EventSource (SSE) | Already used in `chat_module.js` |
| Graph Visualization | D3.js v7 | Industry standard, acceptable new lib |
| API | FastAPI Router | Existing plugin pattern |
| Templates | Jinja2 | Existing template system |
| Styling | Vanilla CSS | No preprocessor needed |
| Data Format | JSON over SSE | Simple, efficient |

### D.5 Event Schema

#### Thought Created Event
```json
{
  "event": "thought_created",
  "session_id": "abc123",
  "data": {
    "thought_number": 5,
    "server_thought_number": 5,
    "thought": "Analyzing the data shows...",
    "is_revision": false,
    "branch_id": "main",
    "timestamp": "2025-01-15T10:30:00Z"
  }
}
```

#### Branch Switched Event
```json
{
  "event": "branch_switched",
  "session_id": "abc123",
  "data": {
    "from_branch": "main",
    "to_branch": "alternative-approach",
    "branch_point": 8,
    "timestamp": "2025-01-15T10:35:00Z"
  }
}
```

### D.6 API Endpoints (Summary)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/plugins/sequential_thinking/panel` | GET | Render panel HTML (iframe) |
| `/plugins/sequential_thinking/sessions` | GET | List all sessions (JSON) |
| `/plugins/sequential_thinking/sessions/{id}` | GET | Session details (JSON) |
| `/plugins/sequential_thinking/sessions/{id}/stream` | GET | SSE stream for session |
| `/plugins/sequential_thinking/sessions/{id}/thoughts` | GET | List thoughts (JSON) |
| `/plugins/sequential_thinking/sessions/{id}/branches` | GET | List branches (JSON) |

### D.7 Dependencies

**New Dependencies to Add**:
- D3.js v7 (CDN or npm): Graph visualization library
  ```html
  <script src="https://d3js.org/d3.v7.min.js"></script>
  ```

**No Other New Dependencies** - everything else uses existing infrastructure.

### D.8 Testing Strategy

1. **Unit Tests** (Python)
   - Test `ThinkingWebFactory.get_web_router()` returns router
   - Test `/sessions` endpoint returns session data
   - Test event emission after thought operations

2. **Integration Tests** (Python)
   - Test SSE stream sends events correctly
   - Test session data consistency
   - Test concurrent SSE connections

3. **E2E Tests** (JavaScript or manual)
   - Test panel opens when button clicked
   - Test session list renders correctly
   - Test thought graph renders
   - Test real-time updates on thought creation
   - Test branch switching updates graph

### D.9 Migration from Design Doc

| Original Design | Revised Implementation |
|----------------|------------------------|
| React components | Vanilla JS functions + D3.js |
| Zustand store | Plain `state` object with Map |
| Custom panel | `PanelManager.createPanel()` |
| WebSocket | EventSource (SSE) |
| npm build process | CDN for D3.js, no build needed |
| State hooks | Direct state mutation + re-render |

### D.10 Next Steps (Revised)

1. ✅ **Design approved** - Use existing infrastructure
2. **Create `web_endpoints.py`** - Backend API
3. **Update `schema.yaml`** - Register UI
4. **Create `templates/panel.html`** - Basic structure
5. **Create `thinking_panel_module.js`** - Frontend logic
6. **Implement D3.js graph** - Visualization
7. **Add SSE events** - Real-time updates
8. **Test integration** - E2E verification
9. **Polish UI/UX** - Styling, responsiveness
10. **Documentation** - Update user guide

**Estimated Effort**: 3-5 days (vs 2-3 weeks with React rewrite)
