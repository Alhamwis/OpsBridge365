# OpsBridge365 Service Desk Build Plan (Days 1-10, TCC tenant)

OpsBridge 365 — Guided Build Plan
From spec to resume-ready portfolio project · August 5 – September 27, 2026
Companion to the OpsBridge 365 Build Specification v1 — keep the spec open while you build; this plan tells you when, the spec tells you how.
Why this schedule
You work overnight full-time, the fall semester is starting, and your job application window is August through November. That means the project cannot wait until December — it needs to be on your resume by late September, while recruiters are still reading it. This plan front-loads the build into roughly eight weeks at 5–8 focused hours per week, with the heaviest weeks before the semester fully ramps up.
The order also has a safety property: after Week 2 you already have something demonstrable (a working intake flow that triages tickets and stamps SLAs), and every week after that adds a finished layer. If life happens and you stall, you still have a real project — not a half-built one.
Timeline at a glance
Week
Dates
Focus
Checkpoint
0
Aug 5 – 9
Access check + site created
You can create a site and a canvas app
1
Aug 10 – 16
Three SharePoint lists + seed assets
Lists match the spec, 15–20 assets seeded
2
Aug 17 – 23
Flow 1 — ticket intake
5 test tickets stamped correctly (all four priorities)
3
Aug 24 – 30
Flows 2, 3, 4
SLA alert, resolution email, and handoff escalation all fire
4
Aug 31 – Sep 6
Power Apps: Home, New ticket, My tickets
You can submit and see your own tickets from the app
5
Sep 7 – 13
Power Apps: Queue, Detail (+ handoff screen)
Full ticket lifecycle works end-to-end in the app
6
Sep 14 – 20
Power BI page + realistic demo data
All 6 KPI cards and 3 visuals show believable numbers
7
Sep 21 – 27
Portfolio packaging + resume update
Screenshots, demo video, README, resume bullets done

Weekly rhythm that works around overnight shifts: two 2–3 hour blocks on days off, plus one short 30-minute session mid-week just to test what you built (create a ticket, watch the flows fire). Short test sessions catch broken flows early, when the fix is still obvious.

Week 0 (Aug 5–9) — Access check
One goal: prove your environment works before you invest a single build hour.
Sign in with your TCC Microsoft 365 account and try to create a SharePoint Team site named OpsBridge365.
Open make.powerapps.com and confirm you can create a blank canvas app.
Open make.powerautomate.com and confirm you can create a cloud flow with the SharePoint connector.
Open Power BI Desktop (free download) on your PC and sign in.
Done when: The OpsBridge365 site exists and you have created (and deleted) a throwaway test app and test flow.
If TCC blocks any of this: first ask your instructor or TCC IT — student tenants often allow it and this is exactly the kind of request they approve. If not, a personal Microsoft 365 Business Basic subscription (about $6/month, or start with the one-month trial) gives you your own tenant with SharePoint, Power Apps, and Power Automate for standard connectors — and screenshots from your own tenant are actually cleaner for a portfolio. Budget one extra weekend if you go this route.
Week 1 (Aug 10–16) — Lists and seed data
Build the three lists exactly as specified. This is the least glamorous week and the most important one: every flow expression and app formula you write later depends on these column names being right.
Create the Tickets list — every column from spec section 1, no spaces in any column name.
Create the Assets list (spec section 2). Remember: Title holds the asset tag.
Create the ShiftHandoffs list (spec section 3).
Seed 15–20 assets with realistic tags (LAP-0001…, PRN-0001…), mixed device types and statuses.
Screenshot each list’s column settings page — this is portfolio evidence and your own backup of the design.
Done when: All three lists match the spec’s column tables exactly, and the Assets list has 15–20 rows.
Watch out: Create every column with its internal name first (AssignedTechnician, not "Assigned Technician") and fix display names later. Use "Date and Time" — never "Date Only" — for anything you will subtract. Getting either wrong means rebuilding columns and re-touching every flow that references them.
Week 2 (Aug 17–23) — Flow 1: ticket intake
The heart of the system: priority matrix, SLA stamping, ticket IDs, routing, and notifications, all in one pass (spec section 5). The spec’s week-one checklist bundles this with the lists — with a full-time job, giving it its own week is the realistic version.
Build the Compose → Switch priority logic from the Impact-Urgency matrix (spec section 4).
Build the ticket ID expression from the item ID (no counter list, no race condition).
Add category routing (hardcoded technician emails for the pilot — you can use test accounts or your own aliases).
Add the Update item step and the three notifications (requester email, Teams adaptive card, P1 direct message).
Create 5 test tickets that cover all four priorities (High/High, High/Medium, Medium/Medium, Low/Low, plus one more).
Done when: Every test ticket has a TicketID, a Priority, both SLA timestamps, an assignee, and Status = Assigned — with zero manual edits.
Watch out: This flow updates the item it was triggered by. Never add a second flow on a plain "item modified" trigger against Tickets, or you will build an infinite loop. That is why Flow 1 does everything in one pass.
Week 3 (Aug 24–30) — Flows 2, 3, and 4
Three smaller flows that turn a ticket tracker into a service desk (spec sections 6–8).
Flow 2 — SLA watchdog: recurrence every 15 minutes, filter query Status ne ’Resolved’ and EscalationSent eq 0, escalate anything due within 30 minutes.
Flow 3 — Resolution: trigger condition on Status = Resolved, stamp ResolvedDate once, email the requester, post to Teams.
Flow 4 — Handoff acknowledgment: build the Title, notify the incoming technician, escalate unacknowledged handoffs after 60 minutes.
Force-test each one: create a ticket with an SLA due in 20 minutes; resolve a ticket; create a handoff and let it sit unacknowledged.
Done when: You have watched all three flows fire on real list items, and a second edit to a Resolved ticket does NOT re-stamp ResolvedDate.
Watch out: Yes/No columns filter on 0 and 1 in OData, not false and true. EscalationSent eq 0 works; EscalationSent eq false fails. This trips up everyone once — let it not cost you an evening.
Week 4 (Aug 31 – Sep 6) — Power Apps, part 1
Generate the app from the Tickets list, then build the requester-facing half (spec section 9). Semester is starting — this week and next are deliberately split so neither is overwhelming.
Home screen: two buttons plus a live count badge of the current user’s open tickets.
New ticket screen: form with ONLY the user-owned fields (Title, Description, Category, Impact, Urgency, Location, AssetTag). Everything flow-owned stays off the form.
My tickets screen: gallery filtered to Requester.Email = User().Email, newest first, with a status pill.
Submit a ticket from the app and confirm Flow 1 stamps it exactly like the manual tests.
Done when: A ticket submitted from your phone via the app comes back triaged, assigned, and SLA-stamped within a minute.
Week 5 (Sep 7–13) — Power Apps, part 2
The technician-facing half — this is what you will demo in interviews.
Technician queue: gallery sorted by Priority then SLAResolutionDue ascending, red border when due within 30 minutes, filter dropdowns for Priority and Status.
Ticket detail: full record, editable Status and Resolution, a Resolve button, and a Reopen button (sets Status back to In Progress, increments ReopenCount, clears ResolvedDate).
Add the sixth screen — the handoff form — only once the five core screens work.
Run a full lifecycle: create → assigned → in progress → resolved → reopened → resolved, all from the app.
Done when: The full lifecycle works end-to-end from the app with no direct list edits, and the queue visibly reprioritizes as SLAs approach.
Week 6 (Sep 14–20) — Power BI + demo data
One page, six KPI cards, three visuals (spec section 10). But first: a dashboard over 12 test tickets looks like a school assignment. Spend the first session generating believable history.
Create 40–60 tickets spread across 3–4 weeks: vary category, priority, and department; resolve about 80% of them, some inside SLA and some breached; leave a few open and one or two reopened. Backdating trick: temporarily point Flow 1’s SLA math at a Created date you set, or simply accept that Created is "now" and vary resolution times instead.
Connect Power BI to the SITE URL (not the list URL), pull all three lists, expand Choice columns to their Value field in Power Query, set every date column to Date/Time.
Build the eight measures from the spec — compute durations in MINUTES and divide by 60 (DATEDIFF in hours truncates, so a 45-minute response reads as zero).
Lay out the page: 6 KPI cards on top, line chart (created vs resolved by week), bar (by category), donut (by priority).
Done when: SLA Compliance % shows a believable number (roughly 75–90%), and you can explain how every card is calculated.

Week 7 (Sep 21–27) — Package it for recruiters
Recruiters cannot log into your tenant. What they can see is what exists in this week’s output — treat it as part of the build, not an afterthought.
Screenshots: every app screen, each flow’s design canvas, the Power BI page, and one Teams channel showing real alert cards.
A 3–5 minute screen recording: submit a ticket, watch it get triaged, work it in the queue, resolve it, show the dashboard. No editing needed — one clean take.
A GitHub repo with a README: what it does, architecture diagram (three lists → four flows → app → dashboard), the priority/SLA matrix, key expressions and DAX, and a "design decisions" section (24/7 SLA clock, EscalationSent anti-spam flag, item-ID-based ticket numbers, single-pass Flow 1 loop guard).
An issues log: 5–10 problems you actually hit and how you fixed them. This is the single best interview-prep artifact you will produce.
Then we write the resume bullets and the project section together, and slot it into your resume for the October–November applications.
Done when: Someone who has never met you can understand what you built, why, and how — in under five minutes — from the repo alone.
Habits that make this a portfolio, not just a project
Screenshot at every milestone, the day it works. Named folders by week. You will never get the "clean early state" back once the system has data in it.
Keep the issues log as you go — one line per problem (what broke, why, the fix). Interviewers ask "tell me about a problem you solved"; you will have ten real answers.
Practice saying the design decisions out loud. The EscalationSent flag is why the watchdog runs every 15 minutes without spamming the channel. The 24/7 SLA clock is a deliberate decision that mirrors your actual operating environment. The item-ID ticket number avoids a counter list and race conditions. These three sentences are the difference between "I followed a tutorial" and "I designed a system."
This project is your overnight job, systematized: shift handoffs, escalations, 24/7 coverage, leadership reporting. When you present it, connect the two — you built the tool for the problem you supervise every night. No other candidate can say that.
If you fall behind
Cut in this order, and the project still stands:
The Graph sync script (Assets LastCheckIn) — already roadmap, not v1. Skip without guilt.
The handoff form screen in Power Apps — handoffs can be created straight in the list; Flow 4 still works.
Power BI visuals — shrink to the six KPI cards only. Cards carry the interview; the donut chart does not.
Flow 4 Part B (the 30-minute acknowledgment checker) — keep Part A notifications.
Do not cut: Flow 1, the SLA watchdog, or the technician queue screen. Those three ARE the project.
What comes after (October onward)
Resume bullets and a one-paragraph project description — I will draft these with you in Week 7.
Optionally, the Azure/Intune add-on we discussed as "later": a small companion piece (for example, device compliance data flowing into the Assets list) that ties the project to your AZ-900/MD-102 prep. Only if the fall schedule allows.
Demo the live system in interviews from your own tenant — five minutes of a working service desk beats any bullet point.
