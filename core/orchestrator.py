import re
from dataclasses import dataclass
from pathlib import Path
from security.permissions import approval_manager
from ai.intent import (
    classify_intent,
    extract_explicit_open_app_target,
    extract_explicit_close_app_target,
    extract_explicit_app_status_target,
    extract_conversational_application_action,
    extract_browser_navigation_target,
    extract_browser_control_action,
    extract_explicit_open_file_target,
    extract_explicit_open_folder_target,
    extract_multi_step_actions,
    extract_conversational_file_action,
    extract_explicit_search_query,
    extract_explicit_file_search_params,
    extract_followup_file_search_clarification,
    check_missing_input,
    AMBIGUOUS_DELETION_PATTERNS,
)
from tools.application import resolve_application
from tools.browser import validate_and_normalize_url, get_browser_controller
from tools.files import discover_files, search_file_contents, resolve_safe_root
import re
from ai.conversation import ConversationManager
from ai.llm import OllamaClient
from ai.schemas import TaskPlan, ProposedAction
from core.planner import validate_plan
from core.executor import PlanExecutor, PlanExecutionResult
from memory.working_memory import WorkingMemory
from memory.long_term_memory import LongTermMemory
from memory.commands import MemoryCommandHandler
from memory.retrieval import MemoryRetriever
from memory.grounding import MemoryGrounder
from memory.models import Memory


@dataclass
class AssistantResult:
    message: str
    plan: TaskPlan | None = None
    retrieved_memories: list[Memory] | None = None
    execution_result: PlanExecutionResult | None = None


class JarvisOrchestrator:
    def __init__(
        self,
        llm: OllamaClient,
        conversation_manager: ConversationManager | None = None,
        long_term_memory: LongTermMemory | None = None,
        memory_retriever: MemoryRetriever | None = None,
        memory_grounder: MemoryGrounder | None = None,
        plan_executor: PlanExecutor | None = None,
        debug_retrieval: bool = False,
    ) -> None:
        self.llm = llm
        self.conversation = conversation_manager or ConversationManager()
        self.memory = WorkingMemory()
        self.ltm = long_term_memory or LongTermMemory()
        self.memory_commands = MemoryCommandHandler(self.ltm)
        self.retriever = memory_retriever or MemoryRetriever(self.ltm)
        self.grounder = memory_grounder or MemoryGrounder()
        self.executor = plan_executor or PlanExecutor()
        self.debug_retrieval = debug_retrieval

    def check_connection(self) -> None:
        self.llm.check_connection()

    def handle(self, user_text: str, allow_approved: bool = False) -> AssistantResult:
        stripped_lower = user_text.strip().lower()

        # Handle explicit approval / rejection commands
        if stripped_lower.startswith("approve ") or stripped_lower.startswith("reject "):
            parts = user_text.strip().split(maxsplit=1)
            cmd = parts[0].lower()
            target_id = parts[1].strip() if len(parts) > 1 else ""

            if cmd == "approve":
                ok, msg = approval_manager.approve_request(target_id)
                if not ok:
                    plan = TaskPlan(intent="approval_failed", message=msg, actions=[], risk_level="low")
                    return AssistantResult(message=msg, plan=plan)

                req = approval_manager.get_request(target_id)
                action = ProposedAction(
                    tool=req.tool,
                    arguments=req.arguments,
                    requires_approval=True,
                    reason=req.explanation,
                    approval_id=req.approval_id,
                )
                plan = TaskPlan(
                    intent="approved_action_execution",
                    message=f"Executing approved action: {req.tool}",
                    actions=[action],
                    risk_level=req.risk_level,
                )
                exec_res = self.executor.execute_plan(plan)
                result_msg = msg
                if exec_res.results and exec_res.results[0].message:
                    result_msg += f"\n{exec_res.results[0].message}"
                return AssistantResult(message=result_msg, plan=plan, execution_result=exec_res)

            elif cmd == "reject":
                ok, msg = approval_manager.reject_request(target_id)
                plan = TaskPlan(intent="approval_rejected", message=msg, actions=[], risk_level="low")
                return AssistantResult(message=msg, plan=plan)

        intent = classify_intent(user_text)
        cleaned_norm = " ".join(re.sub(r"[^\w\s]", " ", user_text.lower()).split())

        # 1. Prompt injection / adversarial override defense
        if intent == "prompt_injection":
            safe_msg = "I can't follow instructions that attempt to override JARVIS's safety rules. Please give me the task normally."
            plan = TaskPlan(
                intent="prompt_injection_blocked",
                message=safe_msg,
                actions=[],
                needs_clarification=False,
                risk_level="low",
            )
            self.memory.add("user", user_text, category="prompt_injection")
            self.memory.add("assistant", safe_msg, category="prompt_injection", plan=plan)
            return AssistantResult(message=safe_msg, plan=plan)

        # 2. Missing essential input (e.g. "run this Python code", "search for")
        elif intent == "missing_input":
            clarification_msg = check_missing_input(cleaned_norm) or "Please provide the details or content needed to run this."
            plan = TaskPlan(
                intent="clarification_needed",
                message=clarification_msg,
                actions=[],
                needs_clarification=True,
                clarification_question=clarification_msg,
                risk_level="low",
            )
            self.memory.add("user", user_text, category="missing_input")
            self.memory.add("assistant", clarification_msg, category="missing_input", plan=plan)
            return AssistantResult(message=clarification_msg, plan=plan)

        # 3. Explicit continuation
        elif intent == "continuation":
            last_task = self.memory.get_last_task()
            if last_task:
                orig_request, prev_plan = last_task
                plan = validate_plan(prev_plan, current_request=orig_request)
                message = f"Continuing previous task: {orig_request}"
                self.memory.add("user", user_text, category="continuation")
                self.memory.add("assistant", message, category="continuation", plan=plan)
                self.memory.set_last_task(orig_request, plan)

                # Execute plan if actions exist
                exec_res = None
                if plan.actions:
                    exec_res = self.executor.execute_plan(plan, allow_approved=allow_approved)

                return AssistantResult(message=message, plan=plan, execution_result=exec_res)
            else:
                message = "Which task would you like me to continue?"
                plan = TaskPlan(
                    intent="clarification_needed",
                    message=message,
                    actions=[],
                    needs_clarification=True,
                    clarification_question=message,
                    risk_level="low",
                )
                self.memory.add("user", user_text, category="unclear")
                self.memory.add("assistant", message, category="unclear", plan=plan)
                return AssistantResult(message=message, plan=plan)

        # 4. Explicit memory commands (Milestone 2B)
        elif intent in {"memory_store", "memory_forget", "memory_recall"}:
            if intent == "memory_store":
                cmd_res = self.memory_commands.handle_store(user_text)
            elif intent == "memory_forget":
                cmd_res = self.memory_commands.handle_forget(user_text)
            else:  # memory_recall
                cmd_res = self.memory_commands.handle_recall(user_text)

            plan = TaskPlan(
                intent=intent,
                message=cmd_res.message,
                actions=[],
                needs_clarification=not cmd_res.success,
                risk_level="low",
            )
            self.memory.add("user", user_text, category=intent)
            self.memory.add("assistant", cmd_res.message, category=intent, plan=plan)
            return AssistantResult(message=cmd_res.message, plan=plan)

        # 5. Vague / Unclear (including ambiguous deletion requests like "start deleting old files")
        elif intent == "unclear":
            is_ambiguous_delete = any(re.search(pat, cleaned_norm) for pat in AMBIGUOUS_DELETION_PATTERNS)
            if is_ambiguous_delete:
                message = "Which files would you like to delete, and where are they located?"
            else:
                message = "Could you please clarify what specific task or item you would like me to help you with?"

            plan = TaskPlan(
                intent="clarification_needed",
                message=message,
                actions=[],
                needs_clarification=True,
                clarification_question=message,
                risk_level="low",
            )
            self.memory.add("user", user_text, category="unclear")
            self.memory.add("assistant", message, category="unclear", plan=plan)
            return AssistantResult(message=message, plan=plan)

        # 6 & 7. Non-memory command paths: retrieve relevant long-term memory
        # Check conversational file action referring to most recently opened file (Milestone 4C.3)
        file_conv_action = extract_conversational_file_action(user_text)
        if file_conv_action is not None:
            last_file = self.memory.get_last_opened_file()
            action_name = file_conv_action.get("action")

            if not last_file:
                resp_msg = "I don't have a recently opened file to determine what \"it\" refers to. Please open or specify a file first."
                plan = TaskPlan(
                    intent="clarification_needed",
                    message=resp_msg,
                    actions=[],
                    needs_clarification=True,
                    clarification_question=resp_msg,
                    risk_level="low",
                )
                self.memory.add("user", user_text, category="conversation")
                self.memory.add("assistant", resp_msg, category="conversation", plan=plan)
                return AssistantResult(message=resp_msg, plan=plan)

            f_name = last_file.get("name", "")
            f_path = last_file.get("path", "")
            f_ext = last_file.get("extension", "").lower()
            p_obj = Path(f_path) if f_path else None

            if action_name == "get_name":
                resp_msg = f"The file is named {f_name}."
                plan = TaskPlan(
                    intent="conversation",
                    message=resp_msg,
                    actions=[],
                    needs_clarification=False,
                    risk_level="low",
                )
                self.memory.add("user", user_text, category="conversation")
                self.memory.add("assistant", resp_msg, category="conversation", plan=plan)
                return AssistantResult(message=resp_msg, plan=plan)

            elif action_name == "get_type":
                ext_clean = f_ext.lstrip(".")
                type_display = ext_clean.upper() if ext_clean else "unknown"
                resp_msg = f"This is a {type_display} file ({f_ext})."
                plan = TaskPlan(
                    intent="conversation",
                    message=resp_msg,
                    actions=[],
                    needs_clarification=False,
                    risk_level="low",
                )
                self.memory.add("user", user_text, category="conversation")
                self.memory.add("assistant", resp_msg, category="conversation", plan=plan)
                return AssistantResult(message=resp_msg, plan=plan)

            elif action_name == "check_type":
                expected_ext = file_conv_action.get("expected_extension", "").lower()
                expected_name = file_conv_action.get("type_name", expected_ext.lstrip(".").upper())
                actual_name = f_ext.lstrip(".").upper() if f_ext else "unknown"

                if f_ext == expected_ext:
                    resp_msg = f"Yes, this is a {expected_name} file."
                else:
                    resp_msg = f"No, this is a {actual_name} file."

                plan = TaskPlan(
                    intent="conversation",
                    message=resp_msg,
                    actions=[],
                    needs_clarification=False,
                    risk_level="low",
                )
                self.memory.add("user", user_text, category="conversation")
                self.memory.add("assistant", resp_msg, category="conversation", plan=plan)
                return AssistantResult(message=resp_msg, plan=plan)

            elif action_name == "get_location":
                parent_dir = str(p_obj.parent) if p_obj else "unknown"
                resp_msg = f"The file is located in:\n\n{parent_dir}"
                plan = TaskPlan(
                    intent="conversation",
                    message=resp_msg,
                    actions=[],
                    needs_clarification=False,
                    risk_level="low",
                )
                self.memory.add("user", user_text, category="conversation")
                self.memory.add("assistant", resp_msg, category="conversation", plan=plan)
                return AssistantResult(message=resp_msg, plan=plan)

            elif action_name == "reveal_location":
                # Stale file safety check: validate file exists before opening location
                resolved_p, err = resolve_safe_root(f_path)
                if err == "UNSAFE_LOCATION" or not resolved_p:
                    resp_msg = "The file location could not be opened safely because it is outside the allowed locations. Allowed locations: Desktop, Documents, Downloads."
                    plan = TaskPlan(
                        intent="clarification_needed",
                        message=resp_msg,
                        actions=[],
                        needs_clarification=True,
                        clarification_question=resp_msg,
                        risk_level="low",
                    )
                    self.memory.add("user", user_text, category="task")
                    self.memory.add("assistant", resp_msg, category="task", plan=plan)
                    return AssistantResult(message=resp_msg, plan=plan)

                file_exists = False
                try:
                    file_exists = resolved_p.exists() and resolved_p.is_file()
                except (PermissionError, OSError):
                    file_exists = False

                if not file_exists:
                    resp_msg = "I remember that file, but it no longer exists at the previously known location."
                    plan = TaskPlan(
                        intent="clarification_needed",
                        message=resp_msg,
                        actions=[],
                        needs_clarification=True,
                        clarification_question=resp_msg,
                        risk_level="low",
                    )
                    self.memory.add("user", user_text, category="task")
                    self.memory.add("assistant", resp_msg, category="task", plan=plan)
                    return AssistantResult(message=resp_msg, plan=plan)

                plan_msg = f"Opening the folder containing {f_name}."
                plan = TaskPlan(
                    intent="reveal_file",
                    message=plan_msg,
                    actions=[
                        ProposedAction(
                            tool="reveal_file",
                            arguments={"path": str(resolved_p)},
                            requires_approval=False,
                            reason=f"Open the folder containing {f_name}",
                        )
                    ],
                    needs_clarification=False,
                    risk_level="low",
                )
                self.memory.add("user", user_text, category="task")
                self.memory.add("assistant", plan_msg, category="task", plan=plan)
                self.memory.set_last_task(user_text, plan)

                exec_res = self.executor.execute_plan(plan, allow_approved=allow_approved)
                return AssistantResult(message=plan_msg, plan=plan, execution_result=exec_res)

        retrieved_memories = self.retriever.retrieve(user_text)

        # Apply deterministic grounding evaluation (Milestone 2D)
        is_fact = self.grounder.is_memory_fact_query(user_text)
        is_sugg = self.grounder.is_suggestion_query(user_text)
        missing_aspect = self.grounder.detect_missing_aspect(user_text, retrieved_memories)

        memory_context = self.grounder.format_grounded_memory_context(
            memories=retrieved_memories,
            is_fact_query=is_fact,
            is_suggestion=is_sugg,
            missing_aspect=missing_aspect,
        )

        if self.debug_retrieval and retrieved_memories:
            print(f"[DEBUG Retrieval] Found {len(retrieved_memories)} relevant memories for query.")

        # 6. Conversation
        if intent == "conversation":
            # Check for follow-up file search clarification (Bug 4)
            followup_clarification = extract_followup_file_search_clarification(user_text)
            last_file_search = self.memory.get_last_file_search()
            if followup_clarification and last_file_search:
                prev_folder = last_file_search.get("folder")
                prev_ext = last_file_search.get("extension")
                prev_exts = last_file_search.get("extensions")
                file_args: dict[str, object] = {
                    "query": followup_clarification,
                }
                if prev_folder:
                    file_args["folder"] = prev_folder
                if prev_ext:
                    file_args["extension"] = prev_ext
                if prev_exts:
                    file_args["extensions"] = prev_exts

                plan_msg = f"Searching for '{followup_clarification}' in your allowed locations."
                if prev_folder:
                    plan_msg = f"Searching for '{followup_clarification}' in {prev_folder}."

                raw_plan = TaskPlan(
                    intent="find_files",
                    message=plan_msg,
                    actions=[
                        ProposedAction(
                            tool="find_files",
                            arguments=file_args,
                            requires_approval=False,
                            reason=f"Follow-up search for '{followup_clarification}' using context from previous search",
                        )
                    ],
                )
                plan = validate_plan(raw_plan, current_request=user_text)
                self.memory.add("user", user_text, category="task")
                self.memory.add("assistant", plan.message, category="task", plan=plan)
                self.memory.set_last_task(user_text, plan)

                exec_res = self.executor.execute_plan(plan, allow_approved=allow_approved)
                return AssistantResult(message=plan.message, plan=plan, execution_result=exec_res)

            answer = self.conversation.respond(
                conversation_history=self.memory.messages,
                current_request=user_text,
                memory_context=memory_context,
            )
            plan = TaskPlan(
                intent="conversation",
                message=answer,
                actions=[],
                needs_clarification=False,
                risk_level="low",
            )
            self.memory.add("user", user_text, category="conversation")
            self.memory.add("assistant", answer, category="conversation", plan=plan)
            return AssistantResult(message=answer, plan=plan, retrieved_memories=retrieved_memories)

        # 7. Dangerous task or standard task
        else:  # intent in {"task", "dangerous_task"}
            # Check for deterministic multi-step actions (Milestone 4D.2) BEFORE single actions
            multi_actions_raw = extract_multi_step_actions(user_text)
            if multi_actions_raw is not None:
                # Milestone 4D.2 Rule: Maximum 3 actions per request
                if len(multi_actions_raw) > 3:
                    limit_msg = f"Your request contains {len(multi_actions_raw)} actions, but I can execute a maximum of 3 actions per request. Please split your request into smaller steps."
                    plan = TaskPlan(
                        intent="clarification_needed",
                        message=limit_msg,
                        actions=[],
                        needs_clarification=True,
                        clarification_question=limit_msg,
                        risk_level="low",
                    )
                    self.memory.add("user", user_text, category=intent)
                    self.memory.add("assistant", limit_msg, category=intent, plan=plan)
                    return AssistantResult(message=limit_msg, plan=plan)

                # Build ordered actions with multi-step application context tracking (Milestone 4D.5)
                # Track the most recent valid application referenced within this multi-step sequence
                simulated_app = self.memory.get_last_application()
                simulated_disp_name = simulated_app["display_name"] if simulated_app else None

                plan_actions: list[ProposedAction] = []
                for act_info in multi_actions_raw:
                    tool_n = act_info.get("tool")
                    args = act_info.get("arguments", {})
                    conv_ref = act_info.get("conversational_reference")

                    if tool_n == "browser_search":
                        plan_actions.append(ProposedAction(
                            tool="browser_search",
                            arguments={"query": args.get("query")},
                            requires_approval=False,
                            reason=f"Search the web for {args.get('query')}",
                        ))
                    elif tool_n == "open_folder":
                        folder_n = args.get("folder")
                        plan_actions.append(ProposedAction(
                            tool="open_folder",
                            arguments={"folder": folder_n},
                            requires_approval=False,
                            reason=f"Open {folder_n} folder",
                        ))
                    elif tool_n == "open_application":
                        app_n = args.get("application")
                        # If this is a valid allowlisted application, update multi-step application context
                        app_res = resolve_application(app_n) if app_n else None
                        if app_res:
                            simulated_disp_name = app_res[1].get("display_name", app_res[0])
                        else:
                            # Invalid or unknown application resets simulated context to avoid accidental target bleed
                            simulated_disp_name = None

                        plan_actions.append(ProposedAction(
                            tool="open_application",
                            arguments={"application": app_n},
                            requires_approval=False,
                            reason=f"Open application {app_n}",
                        ))
                    elif tool_n == "close_application":
                        app_n = args.get("application")
                        if not app_n and conv_ref:
                            # Resolve conversational pronoun to multi-step simulated app
                            if simulated_disp_name:
                                app_n = simulated_disp_name
                            else:
                                app_n = conv_ref  # Will fail safely if unknown/unresolved
                        elif app_n:
                            app_res = resolve_application(app_n)
                            if app_res:
                                simulated_disp_name = app_res[1].get("display_name", app_res[0])

                        plan_actions.append(ProposedAction(
                            tool="close_application",
                            arguments={"application": app_n},
                            requires_approval=False,
                            reason=f"Close application {app_n}",
                        ))
                    elif tool_n == "check_application_running":
                        app_n = args.get("application")
                        if not app_n and conv_ref:
                            if simulated_disp_name:
                                app_n = simulated_disp_name
                            else:
                                app_n = conv_ref
                        elif app_n:
                            app_res = resolve_application(app_n)
                            if app_res:
                                simulated_disp_name = app_res[1].get("display_name", app_res[0])

                        plan_actions.append(ProposedAction(
                            tool="check_application_running",
                            arguments={"application": app_n},
                            requires_approval=False,
                            reason=f"Check if {app_n} is running",
                        ))
                    elif tool_n == "browser_navigate":
                        nav_u = args.get("url")
                        plan_actions.append(ProposedAction(
                            tool="browser_navigate",
                            arguments={"url": nav_u},
                            requires_approval=False,
                            reason=f"Navigate browser to {nav_u}",
                        ))
                    elif tool_n == "browser_go_back":
                        plan_actions.append(ProposedAction(
                            tool="browser_go_back",
                            arguments={},
                            requires_approval=False,
                            reason="Go back to previous page in browser",
                        ))
                    elif tool_n == "browser_go_forward":
                        plan_actions.append(ProposedAction(
                            tool="browser_go_forward",
                            arguments={},
                            requires_approval=False,
                            reason="Go forward to next page in browser",
                        ))
                    elif tool_n == "browser_refresh":
                        plan_actions.append(ProposedAction(
                            tool="browser_refresh",
                            arguments={},
                            requires_approval=False,
                            reason="Refresh current browser page",
                        ))
                    elif tool_n == "browser_get_page_info":
                        plan_actions.append(ProposedAction(
                            tool="browser_get_page_info",
                            arguments=args,
                            requires_approval=False,
                            reason="Retrieve current browser page info",
                        ))

                if plan_actions:
                    plan_msg = f"Executing {len(plan_actions)} requested actions in sequence."
                    raw_plan = TaskPlan(
                        intent="task",
                        message=plan_msg,
                        actions=plan_actions,
                        needs_clarification=False,
                        risk_level="low",
                    )
                    plan = validate_plan(raw_plan, current_request=user_text)
                    self.memory.add("user", user_text, category=intent)
                    self.memory.add("assistant", plan_msg, category=intent, plan=plan)
                    self.memory.set_last_task(user_text, plan)
                    exec_res = self.executor.execute_plan(plan, allow_approved=allow_approved, stop_on_failure=True)
                    # Update application context for successful application actions
                    if exec_res and exec_res.results:
                        for idx_act, act_res in enumerate(exec_res.results):
                            if act_res.executed and act_res.success:
                                if act_res.action_name in {"open_application", "close_application"}:
                                    app_target_name = plan_actions[idx_act].arguments.get("application")
                                    app_res = resolve_application(app_target_name) if app_target_name else None
                                    if app_res:
                                        self.memory.set_last_application({
                                            "key": app_res[0],
                                            "display_name": app_res[1].get("display_name", app_res[0]),
                                        })
                    return AssistantResult(message=plan_msg, plan=plan, execution_result=exec_res)

            # Check for deterministic explicit commands BEFORE calling LLM
            explicit_query = extract_explicit_search_query(user_text)
            explicit_file_params = extract_explicit_file_search_params(user_text)
            followup_clarification = extract_followup_file_search_clarification(user_text)
            open_file_target = extract_explicit_open_file_target(user_text)
            open_folder_target = extract_explicit_open_folder_target(user_text)
            explicit_app = extract_explicit_open_app_target(user_text)
            close_app_target = extract_explicit_close_app_target(user_text)
            app_status_target = extract_explicit_app_status_target(user_text)
            conv_app_action = extract_conversational_application_action(user_text)
            browser_control_action = extract_browser_control_action(user_text)
            browser_nav_target = extract_browser_navigation_target(user_text)

            # Check browser control action (Milestone 5.1: go back, forward, refresh, page info)
            if browser_control_action is not None:
                b_action_name = browser_control_action["action"]
                b_action_args = {}
                if "type" in browser_control_action:
                    b_action_args["type"] = browser_control_action["type"]

                raw_plan = TaskPlan(
                    intent=b_action_name,
                    message=f"Executing {b_action_name}.",
                    actions=[
                        ProposedAction(
                            tool=b_action_name,
                            arguments=b_action_args,
                            requires_approval=False,
                            reason=f"User requested browser action: {b_action_name}",
                        )
                    ],
                )
                plan = validate_plan(raw_plan, current_request=user_text)
                exec_res = self.executor.execute_plan(plan, allow_approved=allow_approved)
                final_msg = plan.message
                if exec_res and exec_res.results:
                    final_msg = exec_res.results[0].message

                self.memory.add("user", user_text, category=intent)
                self.memory.add("assistant", final_msg, category=intent, plan=plan)
                return AssistantResult(message=final_msg, plan=plan, execution_result=exec_res)

            # Check explicit app status query (Milestone 4D.4):
            # Deterministic, non-destructive, direct factual query path
            if app_status_target is not None:
                app_res = resolve_application(app_status_target)
                disp_name = app_res[1].get("display_name", app_res[0]) if app_res else app_status_target
                raw_plan = TaskPlan(
                    intent="check_application_running",
                    message=f"Checking if {disp_name} is running.",
                    actions=[
                        ProposedAction(
                            tool="check_application_running",
                            arguments={"application": disp_name},
                            requires_approval=False,
                            reason=f"Check running status of {disp_name}",
                        )
                    ],
                )
                plan = validate_plan(raw_plan, current_request=user_text)
                exec_res = self.executor.execute_plan(plan, allow_approved=allow_approved)
                # Formulate direct factual response
                final_msg = plan.message
                if exec_res and exec_res.results:
                    final_msg = exec_res.results[0].message

                self.memory.add("user", user_text, category=intent)
                self.memory.add("assistant", final_msg, category=intent, plan=plan)
                return AssistantResult(message=final_msg, plan=plan, execution_result=exec_res)

            # Check conversational application status query (Milestone 4D.5):
            # Resolves pronouns ('it', 'that app', 'the app', 'the application') to last remembered application
            if conv_app_action is not None and conv_app_action.get("action") == "check_application_running":
                last_app = self.memory.get_last_application()
                if not last_app:
                    no_context_msg = 'I don\'t have a recently used application to determine what "it" refers to. Please specify the application.'
                    plan = TaskPlan(
                        intent="clarification_needed",
                        message=no_context_msg,
                        actions=[],
                        needs_clarification=True,
                        clarification_question=no_context_msg,
                        risk_level="low",
                    )
                    self.memory.add("user", user_text, category=intent)
                    self.memory.add("assistant", no_context_msg, category=intent, plan=plan)
                    return AssistantResult(message=no_context_msg, plan=plan)

                disp_name = last_app["display_name"]
                raw_plan = TaskPlan(
                    intent="check_application_running",
                    message=f"Checking if {disp_name} is running.",
                    actions=[
                        ProposedAction(
                            tool="check_application_running",
                            arguments={"application": disp_name},
                            requires_approval=False,
                            reason=f"Check running status of {disp_name}",
                        )
                    ],
                )
                plan = validate_plan(raw_plan, current_request=user_text)
                exec_res = self.executor.execute_plan(plan, allow_approved=allow_approved)
                final_msg = plan.message
                if exec_res and exec_res.results:
                    final_msg = exec_res.results[0].message

                self.memory.add("user", user_text, category=intent)
                self.memory.add("assistant", final_msg, category=intent, plan=plan)
                return AssistantResult(message=final_msg, plan=plan, execution_result=exec_res)

            # Check explicit open file request FIRST (Milestone 4B.6 & 4B.6.2)
            # Deterministic resolution completely bypasses LLM planning to prevent hallucinated actions
            if open_file_target is not None:
                target_type = open_file_target.get("type")
                recent_results = self.memory.get_last_file_results()

                selected_file_path = None
                selection_error_msg = None

                if target_type == "index":
                    idx = open_file_target.get("index")
                    if not recent_results:
                        selection_error_msg = f"I don't have a recent file list to select result {idx} from. Please search for files first or specify the file name."
                    elif idx < 1 or idx > len(recent_results):
                        total_res_count = len(recent_results)
                        selection_error_msg = f"Invalid selection: There are only {total_res_count} recent file result{'s' if total_res_count != 1 else ''}. Please choose a result between 1 and {total_res_count}."
                    else:
                        selected_file_path = recent_results[idx - 1]["path"]

                elif target_type == "relative":
                    direction = open_file_target.get("direction", "next")
                    t_filter = open_file_target.get("target", "file")
                    if not recent_results:
                        selection_error_msg = f"I don't have a recent file list to determine what \"{direction}\" refers to. Please specify the file you want to open."
                    else:
                        current_idx = self.memory.get_current_file_selection_index()
                        if current_idx is None:
                            selection_error_msg = "Please specify which file you want to start with from the current list."
                        else:
                            # If target specifies pdf or document, navigate to next/previous of that type
                            if t_filter in {"pdf", "document"}:
                                def matches_t(r: dict) -> bool:
                                    ext = r.get("extension", "") or Path(r.get("path", "")).suffix.lower()
                                    if t_filter == "pdf":
                                        return ext == ".pdf"
                                    elif t_filter == "document":
                                        return ext in {".pdf", ".docx", ".doc", ".txt", ".rtf", ".odt", ".pptx", ".xlsx"}
                                    return True

                                target_idx = None
                                if direction == "next":
                                    for i in range(current_idx + 1, len(recent_results) + 1):
                                        if matches_t(recent_results[i - 1]):
                                            target_idx = i
                                            break
                                    if target_idx is None:
                                        selection_error_msg = f"You are already at the last {t_filter} in the current selection."
                                else:
                                    for i in range(current_idx - 1, 0, -1):
                                        if matches_t(recent_results[i - 1]):
                                            target_idx = i
                                            break
                                    if target_idx is None:
                                        selection_error_msg = f"You are already at the first {t_filter} in the current selection."

                                if target_idx is not None:
                                    selected_file_path = recent_results[target_idx - 1]["path"]
                            else:
                                if direction == "next":
                                    if current_idx >= len(recent_results):
                                        selection_error_msg = "You are already at the last file in the current selection."
                                    else:
                                        selected_file_path = recent_results[current_idx]["path"]
                                else:
                                    if current_idx <= 1:
                                        selection_error_msg = "You are already at the first file in the current selection."
                                    else:
                                        selected_file_path = recent_results[current_idx - 2]["path"]

                elif target_type == "pronoun":
                    ptype = open_file_target.get("target", "it")
                    is_again = bool(open_file_target.get("again", False))

                    # If user says "open it again" and we have a last opened file, resolve to it safely
                    if is_again and self.memory.get_last_opened_file():
                        last_op = self.memory.get_last_opened_file()
                        selected_file_path = last_op["path"]
                    elif not recent_results:
                        if is_again:
                            selection_error_msg = "No recently opened file found to reopen."
                        else:
                            selection_error_msg = f"I don't have a recent file result to resolve \"{ptype}\" to. Please specify the file you want to open."
                    else:
                        # Filter by type if user specified "that PDF" or "that document"
                        candidate_results = recent_results
                        if ptype == "pdf":
                            pdf_cands = [r for r in recent_results if r.get("extension") == ".pdf" or r.get("name", "").lower().endswith(".pdf")]
                            if pdf_cands:
                                candidate_results = pdf_cands
                        elif ptype == "document":
                            doc_exts = {".pdf", ".docx", ".doc", ".txt", ".rtf", ".odt", ".pptx", ".xlsx"}
                            doc_cands = [r for r in recent_results if r.get("extension") in doc_exts or any(r.get("name", "").lower().endswith(e) for e in doc_exts)]
                            if doc_cands:
                                candidate_results = doc_cands

                        if len(candidate_results) == 1:
                            selected_file_path = candidate_results[0]["path"]
                        elif len(candidate_results) > 1:
                            total_count = self.memory.get_last_file_total_count() if candidate_results == recent_results else len(candidate_results)
                            total_count = max(total_count, len(candidate_results))

                            if total_count > 10:
                                lines = [
                                    f"I found {total_count} matching files, so \"Open it\" is ambiguous.",
                                    "",
                                    "Please specify the file name or result number.",
                                    "",
                                    "For example:",
                                    "• Open result 14",
                                    f"• Open {candidate_results[0]['name']}",
                                    "",
                                    f"Showing the first 10 of {total_count} results:",
                                ]
                                for i, item in enumerate(candidate_results[:10], start=1):
                                    lines.append(f"{i}. {item['name']}")
                                selection_error_msg = "\n".join(lines)
                                # Keep all candidate results selectable by index up to total_count
                                self.memory.set_active_file_selection(
                                    candidate_results,
                                    source="ambiguous_pronoun",
                                    is_clarification=True,
                                    total_count=total_count,
                                )
                            else:
                                lines = ["I found multiple recent files. Please specify which one you want to open:"]
                                for i, item in enumerate(candidate_results, start=1):
                                    lines.append(f"{i}. {item['name']}")
                                selection_error_msg = "\n".join(lines)
                                self.memory.set_active_file_selection(
                                    candidate_results,
                                    source="ambiguous_pronoun",
                                    is_clarification=True,
                                    total_count=len(candidate_results),
                                )
                        else:
                            selection_error_msg = f"No recent {ptype} found to open."

                elif target_type == "descriptive":
                    desc = str(open_file_target.get("description", "")).strip()
                    if not recent_results:
                        selection_error_msg = f"I don't have a recent file list to resolve \"{desc}\" against. Please search for files first."
                    else:
                        desc_lower = desc.lower()
                        # If descriptor is a file type (e.g. "PDF", "Word", "Excel", "Text", "document")
                        desc_exts: list[str] = []
                        if desc_lower in {"pdf", "pdfs"}:
                            desc_exts = [".pdf"]
                        elif desc_lower in {"word", "docx", "doc"}:
                            desc_exts = [".docx", ".doc"]
                        elif desc_lower in {"excel", "xlsx", "spreadsheet"}:
                            desc_exts = [".xlsx", ".xls", ".csv"]
                        elif desc_lower in {"document", "documents"}:
                            desc_exts = [".pdf", ".docx", ".doc", ".txt", ".rtf", ".odt", ".pptx", ".xlsx"]

                        desc_tokens = [t for t in re.sub(r"[\s_()\-.]+", " ", desc_lower).split() if t not in {"the", "a", "an", "file", "document", "one"}]

                        matching_cands = []
                        for r in recent_results:
                            r_name = r.get("name", "")
                            r_lower = r_name.lower()
                            r_ext = r.get("extension", "").lower()
                            # 1. Type match
                            if desc_exts and (r_ext in desc_exts or any(r_lower.endswith(e) for e in desc_exts)):
                                matching_cands.append(r)
                                continue
                            # 2. Token match
                            if desc_tokens:
                                r_norm_tokens = re.sub(r"[\s_()\-.]+", " ", r_lower).split()
                                r_norm_str = re.sub(r"[\s_()\-.]+", " ", r_lower)
                                if all(t in r_norm_tokens or t in r_norm_str for t in desc_tokens):
                                    matching_cands.append(r)

                        if len(matching_cands) == 1:
                            selected_file_path = matching_cands[0]["path"]
                        elif len(matching_cands) > 1:
                            lines = [f"I found multiple files matching '{desc}'. Please specify which one you want to open:"]
                            for i, item in enumerate(matching_cands[:10], start=1):
                                lines.append(f"{i}. {item['name']}")
                            selection_error_msg = "\n".join(lines)
                            self.memory.set_active_file_selection(
                                matching_cands,
                                source=f"descriptive_{desc}",
                                is_clarification=True,
                                total_count=len(matching_cands),
                            )
                        else:
                            selection_error_msg = f"Could not find any recent file matching '{desc}'."

                elif target_type == "filename":
                    fname = str(open_file_target.get("filename", "")).strip()

                    # 1. If an explicit path was supplied (contains / or \ or starts with ..), check safe root containment
                    if "/" in fname or "\\" in fname or fname.startswith(".."):
                        resolved_p, err = resolve_safe_root(fname)
                        if err == "UNSAFE_LOCATION" or not resolved_p:
                            selection_error_msg = "The file could not be opened safely because it is outside the allowed locations. Allowed locations: Desktop, Documents, Downloads."
                        elif resolved_p and resolved_p.is_file():
                            selected_file_path = str(resolved_p)
                        else:
                            selection_error_msg = f"File not found: '{fname}'."
                    else:
                        # 2. Natural query normalization & keyword extraction (Milestone 4B.6.1 & 4C.1)
                        # Check for descriptive formats like "the resume called Akhil", "the resume named Akhil", "the updated resume"
                        clean_fname = fname
                        m_called = re.match(r"^(?:the\s+)?(?:file|document|pdf|word\s+doc(?:ument)?|notes?|resume|presentation|spreadsheet)?\s*(?:called|named)\s+(.+)$", clean_fname, re.I)
                        if m_called:
                            clean_fname = m_called.group(1).strip()

                        base_name = clean_fname.strip()
                        req_ext = None
                        ext_match = re.search(r"\.([a-zA-Z0-9]+)$", base_name)
                        if ext_match:
                            req_ext = "." + ext_match.group(1).lower()
                            base_name = base_name[:ext_match.start()]

                        tokens = re.sub(r"[\s_()\-]+", " ", base_name).lower().split()
                        filler_words = {"my", "the", "file", "files", "document", "documents", "a", "an", "please"}
                        keywords = [t for t in tokens if t not in filler_words]
                        primary_search_term = keywords[0] if len(keywords) == 1 else (" ".join(keywords) if keywords else base_name.lower())

                        def matches_candidate(cand_name: str) -> bool:
                            c_lower = cand_name.lower()
                            r_lower = clean_fname.lower()
                            # 1. Exact match
                            if c_lower == r_lower or c_lower == r_lower.strip():
                                return True
                            # 2. Raw target without extension match
                            c_stem = re.sub(r"\.[a-zA-Z0-9]+$", "", c_lower)
                            if c_stem == r_lower:
                                return True
                            # 3. Separator-normalized match
                            c_norm = re.sub(r"[\s_()\-]+", " ", c_lower)
                            r_norm = re.sub(r"[\s_()\-]+", " ", r_lower)
                            if r_norm in c_norm:
                                return True
                            # 4. Keyword matching (all keywords present)
                            if keywords:
                                c_tokens = re.sub(r"[\s_()\-.]+", " ", c_lower).split()
                                if all(kw in c_lower or any(kw in tok for tok in c_tokens) for kw in keywords):
                                    if req_ext:
                                        return c_lower.endswith(req_ext)
                                    return True
                            return False

                        # Check recent results first
                        recent_matches = [r for r in recent_results if matches_candidate(r.get("name", ""))]

                        # Prioritize exact match if present
                        exact_recent = [r for r in recent_matches if r.get("name", "").lower() == fname.lower()]
                        if len(exact_recent) == 1:
                            selected_file_path = exact_recent[0]["path"]
                        elif len(recent_matches) == 1:
                            selected_file_path = recent_matches[0]["path"]
                        elif len(recent_matches) > 1:
                            clarif_term = primary_search_term if primary_search_term else fname
                            lines = [f"I found multiple files matching '{clarif_term}'. Please specify which one you want to open:"]
                            for i, item in enumerate(recent_matches[:10], start=1):
                                lines.append(f"{i}. {item['name']}")
                            selection_error_msg = "\n".join(lines)
                            # CRITICAL FIX: Replace active selection context with these clarification candidates!
                            self.memory.set_active_file_selection(
                                recent_matches,
                                source=fname,
                                is_clarification=True,
                                total_count=len(recent_matches),
                            )
                        else:
                            # 3. Search safe roots using discover_files
                            disc_res = discover_files(query=primary_search_term, extension=req_ext)
                            # If primary term had 0 matches and was different from raw fname, try raw fname
                            if (not disc_res.success or not disc_res.files) and primary_search_term != fname:
                                disc_res = discover_files(query=fname)

                            if disc_res.success and disc_res.files:
                                # Filter discovered files with candidate matching
                                filtered_cands = [f for f in disc_res.files if matches_candidate(f.name)]
                                cands_to_use = filtered_cands if filtered_cands else disc_res.files

                                cands_dicts = [
                                    {"name": f.name, "path": f.path, "extension": f.extension or ""}
                                    for f in cands_to_use
                                ]
                                self.memory.remember_file_results(cands_dicts)

                                # Exact match check
                                exact_matches = [f for f in cands_to_use if f.name.lower() == fname.lower()]
                                if len(exact_matches) == 1:
                                    selected_file_path = exact_matches[0].path
                                elif len(cands_to_use) == 1:
                                    selected_file_path = cands_to_use[0].path
                                else:
                                    clarif_term = primary_search_term if primary_search_term else fname
                                    lines = [f"I found multiple files matching '{clarif_term}'. Please specify which one you want to open:"]
                                    for i, item in enumerate(cands_to_use[:10], start=1):
                                        lines.append(f"{i}. {item.name}")
                                    selection_error_msg = "\n".join(lines)
                                    # CRITICAL FIX: Replace active selection context with these clarification candidates!
                                    self.memory.set_active_file_selection(
                                        cands_dicts,
                                        source=fname,
                                        is_clarification=True,
                                        total_count=len(cands_dicts),
                                    )
                            else:
                                selection_error_msg = f"Could not find any file named '{fname}' in allowed search locations."

                if selection_error_msg:
                    raw_plan = TaskPlan(
                        intent="clarification_needed",
                        message=selection_error_msg,
                        actions=[],
                        needs_clarification=True,
                        clarification_question=selection_error_msg,
                        risk_level="low",
                    )
                elif selected_file_path:
                    raw_plan = TaskPlan(
                        intent="open_file",
                        message=f"Opening {selected_file_path}.",
                        actions=[
                            ProposedAction(
                                tool="open_file",
                                arguments={"path": selected_file_path},
                                requires_approval=False,
                                reason=f"Open requested file: {selected_file_path}",
                            )
                        ],
                        needs_clarification=False,
                        risk_level="low",
                    )
                else:
                    raw_plan = TaskPlan(
                        intent="clarification_needed",
                        message="Please specify which file or application you would like to open.",
                        actions=[],
                        needs_clarification=True,
                        clarification_question="Please specify which file or application you would like to open.",
                        risk_level="low",
                    )

            # Check explicit local file discovery FIRST before web search (Conceptual Precedence)
            elif explicit_file_params is not None:
                # Deterministic file discovery handling (Milestone 4B, 4B.2, 4B.3, 4B.4):
                # Never modify/delete/execute, strictly read-only within safe search roots
                mode = explicit_file_params.get("mode", "search")

                if mode == "content_search":
                    content_args: dict[str, object] = {
                        "query": explicit_file_params.get("query", ""),
                    }
                    if explicit_file_params.get("location"):
                        content_args["folder"] = explicit_file_params["location"]
                    if explicit_file_params.get("extensions"):
                        content_args["extensions"] = explicit_file_params["extensions"]

                    q_str = content_args["query"]
                    raw_plan = TaskPlan(
                        intent="search_file_contents",
                        message=f"Searching for files containing '{q_str}' in allowed locations.",
                        actions=[
                            ProposedAction(
                                tool="search_file_contents",
                                arguments=content_args,
                                requires_approval=False,
                                reason=f"Search inside local file contents for '{q_str}'",
                            )
                        ],
                    )
                else:
                    file_args: dict[str, object] = {}
                    if explicit_file_params.get("query"):
                        file_args["query"] = explicit_file_params["query"]
                    if explicit_file_params.get("extension"):
                        file_args["extension"] = explicit_file_params["extension"]
                    if explicit_file_params.get("extensions"):
                        file_args["extensions"] = explicit_file_params["extensions"]
                    if explicit_file_params.get("location"):
                        file_args["folder"] = explicit_file_params["location"]
                    if mode == "list":
                        file_args["recursive"] = False
                    if explicit_file_params.get("sort_by"):
                        file_args["sort_by"] = explicit_file_params["sort_by"]
                    if explicit_file_params.get("sort_order"):
                        file_args["sort_order"] = explicit_file_params["sort_order"]
                    if explicit_file_params.get("time_filter"):
                        file_args["time_filter"] = explicit_file_params["time_filter"]
                    if explicit_file_params.get("size_filter"):
                        file_args["size_filter"] = explicit_file_params["size_filter"]

                    is_listing = mode == "list"
                    action_reason = f"List contents of {file_args.get('folder', 'location')}" if is_listing else f"Discover files matching request: {user_text}"
                    plan_msg = f"Listing contents of {file_args.get('folder', 'location')}." if is_listing else "Searching for matching files in your allowed locations."

                    raw_plan = TaskPlan(
                        intent="find_files",
                        message=plan_msg,
                        actions=[
                            ProposedAction(
                                tool="find_files",
                                arguments=file_args,
                                requires_approval=False,
                                reason=action_reason,
                            )
                        ],
                    )
            elif followup_clarification and self.memory.get_last_file_search():
                last_file_search = self.memory.get_last_file_search()
                prev_folder = last_file_search.get("folder")
                prev_ext = last_file_search.get("extension")
                prev_exts = last_file_search.get("extensions")
                file_args: dict[str, object] = {
                    "query": followup_clarification,
                }
                if prev_folder:
                    file_args["folder"] = prev_folder
                if prev_ext:
                    file_args["extension"] = prev_ext
                if prev_exts:
                    file_args["extensions"] = prev_exts

                plan_msg = f"Searching for '{followup_clarification}' in your allowed locations."
                if prev_folder:
                    plan_msg = f"Searching for '{followup_clarification}' in {prev_folder}."

                raw_plan = TaskPlan(
                    intent="find_files",
                    message=plan_msg,
                    actions=[
                        ProposedAction(
                            tool="find_files",
                            arguments=file_args,
                            requires_approval=False,
                            reason=f"Follow-up search for '{followup_clarification}' using context from previous search",
                        )
                    ],
                )
            elif explicit_query is not None:
                # Deterministic search handling:
                # 1. Do NOT launch Chrome separately when searching to avoid duplicate windows (BUG 1 FIX)
                # 2. Preserve the exact literal search query with all special characters (BUG 2 & 3 FIX)
                raw_plan = TaskPlan(
                    intent="browser_search",
                    message=f"I'll search for '{explicit_query}'.",
                    actions=[
                        ProposedAction(
                            tool="browser_search",
                            arguments={"query": explicit_query},
                            requires_approval=False,
                            reason=f"Search the web for {explicit_query}",
                        )
                    ],
                )
            elif open_folder_target is not None:
                # Deterministic folder opening (Milestone 4D.1):
                # Safely open Desktop, Documents, or Downloads without LLM planning
                raw_plan = TaskPlan(
                    intent="open_folder",
                    message=f"Opening {open_folder_target.capitalize()} folder.",
                    actions=[
                        ProposedAction(
                            tool="open_folder",
                            arguments={"folder": open_folder_target},
                            requires_approval=False,
                            reason=f"Open {open_folder_target} folder",
                        )
                    ],
                )
            elif browser_nav_target is not None:
                # Deterministic browser website / URL navigation (Milestone 5.1):
                norm_url, err = validate_and_normalize_url(browser_nav_target)
                target_url = norm_url or browser_nav_target
                raw_plan = TaskPlan(
                    intent="browser_navigate",
                    message=f"Opening {browser_nav_target}.",
                    actions=[
                        ProposedAction(
                            tool="browser_navigate",
                            arguments={"url": target_url},
                            requires_approval=False,
                            reason=f"Navigate browser to {target_url}",
                        )
                    ],
                )
            elif explicit_app is not None:
                # Deterministic application launch handling (Milestone 4D.1):
                app_res = resolve_application(explicit_app)
                disp_name = app_res[1].get("display_name", app_res[0]) if app_res else explicit_app
                raw_plan = TaskPlan(
                    intent="open_application",
                    message=f"Opening {disp_name}.",
                    actions=[
                        ProposedAction(
                            tool="open_application",
                            arguments={"application": disp_name},
                            requires_approval=False,
                            reason=f"User explicitly requested opening {disp_name}",
                        )
                    ],
                )
            elif close_app_target is not None:
                # Deterministic application close handling (Milestone 4D.4):
                app_res = resolve_application(close_app_target)
                disp_name = app_res[1].get("display_name", app_res[0]) if app_res else close_app_target
                raw_plan = TaskPlan(
                    intent="close_application",
                    message=f"Closing {disp_name}.",
                    actions=[
                        ProposedAction(
                            tool="close_application",
                            arguments={"application": disp_name},
                            requires_approval=False,
                            reason=f"User explicitly requested closing {disp_name}",
                        )
                    ],
                )
            elif conv_app_action is not None and conv_app_action.get("action") == "close_application":
                # Conversational application close handling (Milestone 4D.5):
                last_app = self.memory.get_last_application()
                if not last_app:
                    no_context_msg = 'I don\'t have a recently used application to determine what "it" refers to. Please specify the application.'
                    raw_plan = TaskPlan(
                        intent="clarification_needed",
                        message=no_context_msg,
                        actions=[],
                        needs_clarification=True,
                        clarification_question=no_context_msg,
                        risk_level="low",
                    )
                else:
                    disp_name = last_app["display_name"]
                    raw_plan = TaskPlan(
                        intent="close_application",
                        message=f"Closing {disp_name}.",
                        actions=[
                            ProposedAction(
                                tool="close_application",
                                arguments={"application": disp_name},
                                requires_approval=False,
                                reason=f"User requested closing {disp_name} via conversational pronoun reference",
                            )
                        ],
                    )
            else:
                # LLM planning for general tasks (only reached if no deterministic file/search/folder/app command matched)
                raw_plan = self.llm.create_plan(
                    conversation_history=self.memory.messages,
                    current_request=user_text,
                    memory_context=memory_context,
                )

            plan = validate_plan(raw_plan, current_request=user_text)

            if plan.needs_clarification and plan.clarification_question:
                message = plan.clarification_question
            else:
                message = plan.message

            self.memory.add("user", user_text, category=intent)
            self.memory.add("assistant", message, category=intent, plan=plan)
            if plan.actions:
                self.memory.set_last_task(user_text, plan)

            # Execute plan actions safely through PlanExecutor
            exec_res = None
            if plan.actions:
                exec_res = self.executor.execute_plan(plan, allow_approved=allow_approved)
                # If the executed action was a file discovery or content search, remember the results in working memory
                for action in plan.actions:
                    if action.tool == "find_files":
                        args = action.arguments
                        disc_files = discover_files(
                            query=args.get("query"),
                            extension=args.get("extension"),
                            extensions=args.get("extensions"),
                            location=args.get("folder") or args.get("path") or args.get("location"),
                            recursive=args.get("recursive", True),
                            sort_by=args.get("sort_by"),
                            sort_order=args.get("sort_order"),
                            time_filter=args.get("time_filter"),
                            size_filter=args.get("size_filter"),
                        )
                        if disc_files.success and disc_files.files:
                            total_disc_count = len(disc_files.files)
                            m_count = re.search(r"Found (\d+) files", disc_files.message)
                            if m_count:
                                total_disc_count = int(m_count.group(1))
                            self.memory.remember_file_results(
                                [
                                    {"name": f.name, "path": f.path, "extension": f.extension or ""}
                                    for f in disc_files.files
                                ],
                                total_count=total_disc_count,
                            )
                    elif action.tool == "search_file_contents":
                        cnt_res = search_file_contents(
                            query=str(action.arguments.get("query", "")),
                            location=action.arguments.get("folder"),
                            extensions=action.arguments.get("extensions"),
                        )
                        if cnt_res.success and cnt_res.matches:
                            self.memory.remember_file_results([
                                {"name": m.name, "path": m.path, "extension": Path(m.path).suffix.lower()}
                                for m in cnt_res.matches
                            ])
                    elif action.tool == "open_file":
                        # Record successfully requested open file path in memory for follow-ups (e.g. "open it again")
                        p_val = action.arguments.get("path")
                        if p_val and isinstance(p_val, str):
                            p_obj = Path(p_val)
                            self.memory.set_last_opened_file({
                                "name": p_obj.name,
                                "path": str(p_obj),
                                "extension": p_obj.suffix.lower() if p_obj.suffix else "",
                            })
                            # Also update current_index in active selection context if this file belongs to it
                            active_sel = self.memory.get_active_file_selection()
                            if active_sel and active_sel.get("candidates"):
                                p_norm = str(p_obj).lower()
                                matched_idx = None
                                for c_i, cand in enumerate(active_sel["candidates"], start=1):
                                    if cand.get("path", "").lower() == p_norm:
                                        matched_idx = c_i
                                        break
                                if matched_idx is not None:
                                    self.memory.set_current_file_selection_index(matched_idx)
                    elif action.tool in {"open_application", "close_application"}:
                        # Record successfully executed application in memory for conversational follow-ups (Milestone 4D.5)
                        # Check that this action actually succeeded
                        action_successful = False
                        if exec_res and exec_res.results:
                            for a_res in exec_res.results:
                                if a_res.action_name == action.tool and a_res.arguments.get("application") == action.arguments.get("application"):
                                    if a_res.executed and a_res.success:
                                        action_successful = True
                                    break
                        if action_successful:
                            app_name_arg = action.arguments.get("application")
                            app_res = resolve_application(app_name_arg) if app_name_arg else None
                            if app_res:
                                self.memory.set_last_application({
                                    "key": app_res[0],
                                    "display_name": app_res[1].get("display_name", app_res[0]),
                                })

            return AssistantResult(
                message=message,
                plan=plan,
                retrieved_memories=retrieved_memories,
                execution_result=exec_res,
            )
