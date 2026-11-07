from core.orchestrator import JarvisOrchestrator
from ai.llm import OllamaClient
from config import settings


def main() -> None:
    print("=" * 58)
    print(" J.A.R.V.I.S. — Local Personal Assistant")
    print(" Milestone 3: Safe Action Execution")
    print("=" * 58)
    print(f"Model: {settings.ollama_model}")
    print("Type 'exit' or 'quit' to stop.\n")

    try:
        assistant = JarvisOrchestrator(OllamaClient())
        assistant.check_connection()
    except Exception as exc:
        print("\n[Startup error] Could not connect to Ollama.")
        print(f"Details: {exc}")
        print("\nCheck that Ollama is installed and running, then run:")
        print(f"  ollama pull {settings.ollama_model}")
        print("  ollama serve")
        return

    while True:
        try:
            user_text = input("\nYou: ").strip()
            if user_text.lower() in {"exit", "quit"}:
                print("JARVIS: Shutting down. See you next time.")
                break
            if not user_text:
                continue

            result = assistant.handle(user_text)
            print("\nJARVIS:")
            print(result.message)

            if result.plan and result.plan.actions:
                # If execution took place
                if result.execution_result and result.execution_result.results:
                    print("\nExecution:")
                    unimplemented_tools: list[str] = []
                    for action_res in result.execution_result.results:
                        if action_res.executed:
                            if action_res.success:
                                print(f"  ✓ {action_res.action_name} completed successfully")
                                # For discovery/search actions that produce formatted content:
                                if action_res.message and action_res.action_name in {"find_files", "search_file_contents"}:
                                    for line in action_res.message.splitlines():
                                        print(f"    {line}")
                            else:
                                print(f"  ✗ {action_res.action_name} failed")
                                if action_res.message:
                                    for line in action_res.message.splitlines():
                                        print(f"    {line}")
                        else:
                            # 1. Skipped due to previous action failure (Milestone 4D.3)
                            if action_res.error == "PREVIOUS_ACTION_FAILED":
                                print(f"  ⊘ {action_res.action_name} skipped")
                                if action_res.message:
                                    for line in action_res.message.splitlines():
                                        print(f"    {line}")
                            # 2. Held for approval
                            elif action_res.requires_approval or action_res.error == "APPROVAL_REQUIRED":
                                app_id = (action_res.arguments or {}).get("approval_id", "")
                                id_info = f" (ID: {app_id} - run 'approve {app_id}' to execute or 'reject {app_id}')" if app_id else ""
                                print(f"  ! {action_res.action_name} [APPROVAL REQUIRED - Execution held]{id_info}")
                            # 3. Genuinely unimplemented tool
                            elif action_res.error == "EXECUTION_NOT_IMPLEMENTED":
                                print(f"  - {action_res.action_name}: {action_res.message}")
                                unimplemented_tools.append(action_res.action_name)
                            # 4. Other unexecuted reasons (e.g. unregistered tool)
                            else:
                                print(f"  - {action_res.action_name}: {action_res.message}")

                    if unimplemented_tools:
                        print(f"\n[Execution for {', '.join(unimplemented_tools)} is not implemented yet.]")

                else:
                    print("\nProposed plan:")
                    for index, action in enumerate(result.plan.actions, start=1):
                        approval = " [APPROVAL REQUIRED]" if action.requires_approval else ""
                        if action.requires_approval and action.approval_id:
                            approval += f" (ID: {action.approval_id} - run 'approve {action.approval_id}' or 'reject {action.approval_id}')"
                        print(f"  {index}. {action.tool}{approval}")
                        if action.arguments:
                            for key, value in action.arguments.items():
                                print(f"       {key}: {value}")
                    print("\n[Planning-only mode: no actions were executed.]")
            else:
                print("\nNo computer actions are needed for this request.")

        except KeyboardInterrupt:
            print("\nJARVIS: Shutting down.")
            break
        except Exception as exc:
            print(f"\nJARVIS: I encountered an error: {exc}")


if __name__ == "__main__":
    main()
