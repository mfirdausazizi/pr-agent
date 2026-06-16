import copy
import datetime
import importlib
import traceback
from functools import partial
from types import SimpleNamespace
from typing import List, Tuple

from jinja2 import Environment, StrictUndefined

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.ensemble import (EnsembleConfig, ensemble_footer,
                                    gather_ensemble_predictions,
                                    resolve_ensemble_config)
from pr_agent.algo.pr_processing import (add_ai_metadata_to_diff_files,
                                         get_pr_diff,
                                         retry_with_fallback_models)
from pr_agent.algo.repo_context.line_validator import validate_key_issues_to_review
from pr_agent.algo.repo_context.prompt_formatter import format_repo_context, reserve_diff_tokens
from pr_agent.algo.repo_context.related_prs import build_related_pr_external_repos
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.algo.utils import (ModelType, PRReviewHeader,
                                 convert_to_markdown_v2, github_action_output,
                                 get_max_tokens, load_yaml,
                                 show_relevant_configurations)
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider_with_context
from pr_agent.git_providers.git_provider import (IncrementalPR,
                                                 get_main_pr_language)
from pr_agent.log import get_logger
from pr_agent.servers.help import HelpMessage
from pr_agent.tools.ticket_pr_compliance_check import extract_and_cache_pr_tickets


class PRReviewer:
    """
    The PRReviewer class is responsible for reviewing a pull request and generating feedback using an AI model.
    """

    def __init__(self, pr_url: str, is_answer: bool = False, is_auto: bool = False, args: list = None,
                 ai_handler: partial[BaseAiHandler, ] = LiteLLMAIHandler):
        """
        Initialize the PRReviewer object with the necessary attributes and objects to review a pull request.

        Args:
            pr_url (str): The URL of the pull request to be reviewed.
            is_answer (bool, optional): Indicates whether the review is being done in answer mode. Defaults to False.
            is_auto (bool, optional): Indicates whether the review is being done in automatic mode. Defaults to False.
            ai_handler (BaseAiHandler): The AI handler to be used for the review. Defaults to None.
            args (list, optional): List of arguments passed to the PRReviewer class. Defaults to None.
        """
        self.git_provider = get_git_provider_with_context(pr_url)
        self.args = args
        self.incremental = self.parse_incremental(args)  # -i command
        if self.incremental and self.incremental.is_incremental:
            self.git_provider.get_incremental_commits(self.incremental)

        self.main_language = get_main_pr_language(
            self.git_provider.get_languages(), self.git_provider.get_files()
        )
        self.pr_url = pr_url
        self.is_answer = is_answer
        self.is_auto = is_auto

        if self.is_answer and not self.git_provider.is_supported("get_issue_comments"):
            raise Exception(f"Answer mode is not supported for {get_settings().config.git_provider} for now")
        self.ai_handler = ai_handler()
        self.ai_handler.main_pr_language = self.main_language
        self.patches_diff = None
        self.prediction = None
        self.ensemble_models_used = []
        self.ensemble_consolidator = ""
        self.ensemble_consolidated = False
        self.repo_context_bundle = None
        self.repo_context_workspace = None
        answer_str, question_str = self._get_user_answers()
        self.pr_description, self.pr_description_files = (
            self.git_provider.get_pr_description(split_changes_walkthrough=True))
        if (self.pr_description_files and get_settings().get("config.is_auto_command", False) and
                get_settings().get("config.enable_ai_metadata", False)):
            add_ai_metadata_to_diff_files(self.git_provider, self.pr_description_files)
            get_logger().debug("AI metadata added to the this command")
        else:
            get_settings().set("config.enable_ai_metadata", False)
            get_logger().debug("AI metadata is disabled for this command")

        self.vars = {
            "title": self.git_provider.pr.title,
            "branch": self.git_provider.get_pr_branch(),
            "description": self.pr_description,
            "language": self.main_language,
            "diff": "",  # empty diff for initial calculation
            "num_pr_files": self.git_provider.get_num_of_files(),
            "num_max_findings": get_settings().pr_reviewer.num_max_findings,
            "require_score": get_settings().pr_reviewer.require_score_review,
            "require_tests": get_settings().pr_reviewer.require_tests_review,
            "require_estimate_effort_to_review": get_settings().pr_reviewer.require_estimate_effort_to_review,
            "require_estimate_contribution_time_cost": get_settings().pr_reviewer.require_estimate_contribution_time_cost,  # noqa: E501
            'require_can_be_split_review': get_settings().pr_reviewer.require_can_be_split_review,
            'require_security_review': get_settings().pr_reviewer.require_security_review,
            'require_todo_scan': get_settings().pr_reviewer.get("require_todo_scan", False),
            'question_str': question_str,
            'answer_str': answer_str,
            "extra_instructions": get_settings().pr_reviewer.extra_instructions,
            "commit_messages_str": self.git_provider.get_commit_messages(),
            "custom_labels": "",
            "enable_custom_labels": get_settings().config.enable_custom_labels,
            "is_ai_metadata": get_settings().get("config.enable_ai_metadata", False),
            "related_tickets": get_settings().get('related_tickets', []),
            'duplicate_prompt_examples': get_settings().config.get('duplicate_prompt_examples', False),
            "date": datetime.datetime.now().strftime('%Y-%m-%d'),
            "repo_context": "",
            "repo_context_status": "unavailable",
            "consolidation_verification_context": "",
        }

        self.token_handler = TokenHandler(
            self.git_provider.pr,
            self.vars,
            get_settings().pr_review_prompt.system,
            get_settings().pr_review_prompt.user
        )

    def _build_repo_context_bundle(self):
        try:
            try:
                workspace_manager_module = importlib.import_module("pr_agent.algo.repo_context.workspace_manager")
            except ModuleNotFoundError:
                workspace_manager_module = importlib.import_module("pr_agent.algo.repo_context.workspace")
            context_builder_module = importlib.import_module("pr_agent.algo.repo_context.context_builder")
            RepoWorkspaceManager = workspace_manager_module.RepoWorkspaceManager
            RepoContextBuilder = context_builder_module.RepoContextBuilder
            repo_context_settings = get_settings().repo_context
            external_repositories = repo_context_settings.get("external_repositories",
                                                              repo_context_settings.get("cross_repos", []))
            allowed_external_repo_urls = repo_context_settings.get("allowed_external_repo_urls", [])
            external_repositories = self._merge_related_pr_external_repositories(
                external_repositories,
                allowed_external_repo_urls,
                repo_context_settings,
            )
            if not allowed_external_repo_urls:
                allowed_external_repo_urls = [repo.get("url") for repo in external_repositories if repo.get("url")]

            workspace_manager = self._instantiate_repo_context_component(RepoWorkspaceManager, [
                {
                    "external_repos": external_repositories if repo_context_settings.get("include_external_repos", False) else [],  # noqa: E501
                    "allowed_external_repo_urls": allowed_external_repo_urls,
                    "include_external_repos": repo_context_settings.get("external_include_repositories",
                                                                        repo_context_settings.get("include_repositories", [])),  # noqa: E501
                    "exclude_external_repos": repo_context_settings.get("external_exclude_repositories",
                                                                        repo_context_settings.get("exclude_repositories", [])),  # noqa: E501
                    "max_external_repos": repo_context_settings.get(
                        "max_external_repositories",
                        repo_context_settings.get("max_external_repos", 5),
                    ),
                    "checkout_timeout_sec": repo_context_settings.get("checkout_timeout_sec",
                                                                      repo_context_settings.get("timeout", 30)),
                    "fallback_to_diff_only": repo_context_settings.get("fallback_to_diff_only", True),
                    "exclude_globs": repo_context_settings.get("excluded_globs",
                                                               repo_context_settings.get("exclude_globs", [])),
                    "max_file_bytes": repo_context_settings.get("max_file_bytes", 200000),
                },
                {"git_provider": self.git_provider, "settings": repo_context_settings},
                {"git_provider": self.git_provider},
                {"pr_url": self.pr_url, "settings": repo_context_settings},
                {},
            ])
            workspace_session = self._open_repo_context_workspace(workspace_manager)
            diff_files = self.git_provider.get_diff_files()
            searcher = self._build_repo_context_searcher(workspace_session)
            builder = self._instantiate_repo_context_component(RepoContextBuilder, [
                {"workspace_session": workspace_session, "searcher": searcher, "fail_open": False},
                {"workspace_session": workspace_session, "searcher": searcher},
                {"workspace_session": workspace_session, "settings": get_settings().repo_context},
                {"workspace": workspace_session},
                {},
            ])
            bundle = builder.build(
                diff_files=diff_files,
                max_agent_rounds=get_settings().repo_context.get("max_agent_rounds", 1),
            )
            self.vars.update(self._format_repo_context_vars(get_settings().config.model, bundle))
            return bundle
        except Exception as e:
            if get_settings().repo_context.get("fallback_to_diff_only", True):
                get_logger().warning("Repo context unavailable, continuing with diff-only review",
                                     artifact={"error": e, "traceback": traceback.format_exc()})
                self.vars["repo_context"] = ""
                self.vars["repo_context_status"] = "unavailable"
                self.vars["consolidation_verification_context"] = ""
                return SimpleNamespace(status="unavailable", reason=str(e), snippets=[], cleanup=lambda: None)
            raise

    def _merge_related_pr_external_repositories(
            self,
            external_repositories,
            allowed_external_repo_urls,
            repo_context_settings,
    ) -> list:
        external_repositories = list(external_repositories or [])
        if not repo_context_settings.get("include_related_prs", False):
            return external_repositories

        identity = self.git_provider.get_repo_context_identity()
        related_repositories = build_related_pr_external_repos(
            self.git_provider.get_user_description(),
            allowed_external_repo_urls,
            current_repo=identity.get("repo"),
            current_pr_num=identity.get("pr_num"),
            max_related_prs=int(repo_context_settings.get("max_related_prs", 3)),
        )
        if not related_repositories:
            return external_repositories

        related_urls = {repo["url"] for repo in related_repositories}
        merged = related_repositories + [
            repo for repo in external_repositories if repo.get("url") not in related_urls
        ]
        get_logger().info("Detected related PR repo context", artifact={"external_repositories": related_repositories})
        return merged

    def _instantiate_repo_context_component(self, component, candidates):
        last_error = None
        for kwargs in candidates:
            try:
                return component(**kwargs)
            except TypeError as e:
                last_error = e
        raise last_error

    def _open_repo_context_workspace(self, workspace_manager):
        create_session = getattr(workspace_manager, "create_session", None)
        if callable(create_session):
            workspace_context = create_session(self.git_provider)
            workspace_session = workspace_context.__enter__()
            self.repo_context_workspace = workspace_context
            return workspace_session
        for method_name in ("open", "create", "create_session", "checkout", "get_session"):
            method = getattr(workspace_manager, method_name, None)
            if callable(method):
                workspace_session = method()
                self.repo_context_workspace = workspace_manager
                return workspace_session
        self.repo_context_workspace = workspace_manager
        return workspace_manager

    def _build_repo_context_searcher(self, workspace_session):
        for module_name, class_names in (
                ("pr_agent.algo.repo_context.searcher", ("RepoContextSearcher", "RepositorySearcher")),
                ("pr_agent.algo.repo_context.symbol_extractor", ("RepoContextSearcher", "RepositorySearcher"))):
            try:
                module = importlib.import_module(module_name)
            except ModuleNotFoundError:
                continue
            for class_name in class_names:
                searcher_class = getattr(module, class_name, None)
                if searcher_class:
                    return self._instantiate_repo_context_component(searcher_class, [
                        {"workspace_session": workspace_session, "settings": get_settings().repo_context},
                        {"workspace_session": workspace_session},
                        {"workspace": workspace_session},
                        {},
                    ])
        return SimpleNamespace(
            find_symbols=lambda path, start_line=None, end_line=None: [],
            find_references=lambda symbol, limit=5: [],
            find_importers=lambda path, limit=5: [],
            find_tests=lambda path, limit=5: [],
            search_text=lambda query, limit=5: [],
        )

    def _cleanup_repo_context_workspace(self):
        for target in (self.repo_context_bundle, self.repo_context_workspace):
            cleanup = getattr(target, "cleanup", None)
            if callable(cleanup):
                try:
                    cleanup()
                except Exception as e:
                    get_logger().warning(f"Failed to cleanup repo context workspace: {e}")
            exit_context = getattr(target, "__exit__", None)
            if callable(exit_context):
                try:
                    exit_context(None, None, None)
                except Exception as e:
                    get_logger().warning(f"Failed to close repo context workspace: {e}")
        self.repo_context_bundle = None
        self.repo_context_workspace = None

    def _format_repo_context_vars(self, model: str, bundle=None) -> dict:
        variables = copy.deepcopy(self.vars)
        bundle = bundle if bundle is not None else self.repo_context_bundle
        repo_context, verification_context, status = self._render_repo_context_bundle(bundle, model)
        variables["repo_context"] = repo_context
        variables["repo_context_status"] = status
        variables["consolidation_verification_context"] = verification_context
        return variables

    def _render_repo_context_bundle(self, bundle, model: str) -> Tuple[str, str, str]:
        if not bundle or getattr(bundle, "status", "unavailable") != "ok":
            return "", "", "unavailable"
        formatted = self._format_repo_context_with_budget(bundle, model, include_verification=False)
        if formatted[0]:
            return formatted[0], self._format_repo_context_with_budget(
                bundle, model, only_verification=True)[0], formatted[1]
        for method_name in ("format_for_model", "render_for_model", "to_prompt"):
            method = getattr(bundle, method_name, None)
            if callable(method):
                rendered = method(model)
                if isinstance(rendered, tuple):
                    repo_context = rendered[0] if len(rendered) > 0 else ""
                    verification_context = rendered[1] if len(rendered) > 1 else ""
                    return repo_context, verification_context, "ok"
                return str(rendered or ""), self._render_verification_context(bundle), "ok"
        return self._render_snippets(getattr(bundle, "snippets", []), include_verification=False), \
            self._render_verification_context(bundle), "ok"

    def _format_repo_context_with_budget(
            self,
            bundle,
            model: str,
            include_verification: bool = True,
            only_verification: bool = False,
    ) -> Tuple[str, str]:
        snippets = list(getattr(bundle, "snippets", []) or [])
        if only_verification:
            snippets = [snippet for snippet in snippets if getattr(snippet, "context_type", "") == "verification"]
        elif not include_verification:
            snippets = [snippet for snippet in snippets if getattr(snippet, "context_type", "") != "verification"]
        max_snippets = get_settings().repo_context.get(
            "max_snippets",
            get_settings().repo_context.get("max_context_snippets", len(snippets)),
        )
        snippets = snippets[: max(0, int(max_snippets))]
        bundle_for_prompt = SimpleNamespace(status="ok", snippets=snippets, reason=getattr(bundle, "reason", ""))
        token_handler = TokenHandler()
        max_tokens = self._repo_context_token_budget(model, token_handler)
        if max_tokens <= 0:
            return "", "partial"
        include_external = get_settings().repo_context.get("include_external_context", True)
        return format_repo_context(
            bundle_for_prompt,
            token_handler,
            max_tokens=max_tokens,
            include_external=include_external)

    def _repo_context_token_budget(self, model: str, token_handler) -> int:
        requested_context_tokens = int(get_settings().repo_context.get("max_context_tokens", 2000))
        max_context_ratio = float(get_settings().repo_context.get("max_context_token_ratio", 0.15))
        min_diff_tokens_reserved = int(get_settings().repo_context.get("min_diff_tokens_reserved", 4000))
        try:
            total_tokens = get_max_tokens(model)
        except Exception:
            total_tokens = int(get_settings().config.get("max_model_tokens", 32000) or 32000)
        ratio_budget = max(0, int(total_tokens * max_context_ratio))
        requested_context_tokens = min(requested_context_tokens, ratio_budget or requested_context_tokens)
        return reserve_diff_tokens(total_tokens, requested_context_tokens, min_diff_tokens_reserved)

    def _render_verification_context(self, bundle) -> str:
        return self._render_snippets(getattr(bundle, "snippets", []), include_verification=True, only_verification=True)

    def _render_snippets(self, snippets, include_verification: bool = True, only_verification: bool = False) -> str:
        rendered_snippets = []
        for snippet in snippets or []:
            context_type = getattr(snippet, "context_type", "")
            if only_verification and context_type != "verification":
                continue
            if not include_verification and context_type == "verification":
                continue
            repo_label = getattr(snippet, "repo_label", None) or getattr(snippet, "repo", "primary")
            path = getattr(snippet, "path", "")
            start = getattr(snippet, "start", None)
            end = getattr(snippet, "end", None)
            content = getattr(snippet, "content", "")
            line_ref = f": {start}-{end}" if start and end else ""
            rendered_snippets.append(f"  ## {repo_label}: {path}{line_ref}\n```\n{content}\n```")
        return "\n\n".join(rendered_snippets)

    def _build_token_handler(self, variables: dict, system: str, user: str):
        return TokenHandler(self.git_provider.pr, variables, system, user)

    def parse_incremental(self, args: List[str]):
        is_incremental = False
        if args and len(args) >= 1:
            arg = args[0]
            if arg == "-i":
                is_incremental = True
        incremental = IncrementalPR(is_incremental)
        return incremental

    async def run(self) -> None:
        try:
            if not self.git_provider.get_files():
                get_logger().info(f"PR has no files: {self.pr_url}, skipping review")
                return None

            if self.incremental.is_incremental and not self._can_run_incremental_review():
                return None

            # if isinstance(self.args, list) and self.args and self.args[0] == 'auto_approve':
            #     get_logger().info(f'Auto approve flow PR: {self.pr_url} ...')
            #     self.auto_approve_logic()
            #     return None

            get_logger().info(f'Reviewing PR: {self.pr_url} ...')
            relevant_configs = {'pr_reviewer': dict(get_settings().pr_reviewer),
                                'config': dict(get_settings().config)}
            get_logger().debug("Relevant configs", artifacts=relevant_configs)

            # ticket extraction if exists
            await extract_and_cache_pr_tickets(self.git_provider, self.vars)
            if get_settings().get("repo_context.enabled", False):
                self.repo_context_bundle = self._build_repo_context_bundle()

            if self.incremental.is_incremental and hasattr(
                    self.git_provider, "unreviewed_files_set") and not self.git_provider.unreviewed_files_set:
                get_logger().info(f"Incremental review is enabled for {self.pr_url} but there are no new files")
                previous_review_url = ""
                if hasattr(self.git_provider, "previous_review"):
                    previous_review_url = self.git_provider.previous_review.html_url
                if get_settings().config.publish_output:
                    self.git_provider.publish_comment(
                        "Incremental Review Skipped\n" f"No files were changed since the [previous PR Review]({previous_review_url})")  # noqa: E501
                return None

            if get_settings().config.publish_output and not get_settings().config.get('is_auto_command', False):
                self.git_provider.publish_comment("Preparing review...", is_temporary=True)

            ensemble_config = resolve_ensemble_config("pr_reviewer")
            if ensemble_config:
                await self._prepare_prediction_ensemble(ensemble_config)
            else:
                await retry_with_fallback_models(self._prepare_prediction, model_type=ModelType.REGULAR)
            if not self.prediction:
                self.git_provider.remove_initial_comment()
                return None

            pr_review = self._prepare_pr_review()
            get_logger().debug("PR output", artifact=pr_review)

            should_publish = get_settings().config.publish_output and self._should_publish_review_no_suggestions(pr_review)  # noqa: E501
            if not should_publish:
                reason = "Review output is not published"
                if get_settings().config.publish_output:
                    reason += ": no major issues detected."
                get_logger().info(reason)
                get_settings().data = {"artifact": pr_review}
                return

            # publish the review
            if get_settings().pr_reviewer.persistent_comment and not self.incremental.is_incremental:
                final_update_message = get_settings().pr_reviewer.final_update_message
                self.git_provider.publish_persistent_comment(pr_review,
                                                             initial_header=f"{PRReviewHeader.REGULAR.value} 🔍",
                                                             update_header=True,
                                                             final_update_message=final_update_message, )
            else:
                self.git_provider.publish_comment(pr_review)

            self.git_provider.remove_initial_comment()
        except Exception as e:
            get_logger().error(f"Failed to review PR: {e}")
        finally:
            self._cleanup_repo_context_workspace()

    def _should_publish_review_no_suggestions(self, pr_review: str) -> bool:
        return get_settings().pr_reviewer.get('publish_output_no_suggestions', True) or "No major issues detected" not in pr_review  # noqa: E501

    async def _prepare_prediction(self, model: str) -> None:
        variables = self._format_repo_context_vars(model)
        token_handler = self._build_token_handler(variables,
                                                  get_settings().pr_review_prompt.system,
                                                  get_settings().pr_review_prompt.user)
        self.patches_diff = get_pr_diff(self.git_provider,
                                        token_handler,
                                        model,
                                        add_line_numbers_to_hunks=True,
                                        disable_extra_lines=False, )

        if self.patches_diff:
            get_logger().debug("PR diff", diff=self.patches_diff)
            self.prediction = await self._get_prediction(model, variables)
        else:
            get_logger().warning(f"Empty diff for PR: {self.pr_url}")
            self.prediction = None

    async def _get_prediction(self, model: str, variables: dict = None) -> str:
        return await self._get_prediction_for_diff_compat(model, self.patches_diff, variables)

    async def _get_prediction_for_diff_compat(self, model: str, patches_diff: str, variables: dict = None) -> str:
        try:
            return await self._get_prediction_for_diff(model, patches_diff, variables)
        except TypeError as e:
            if variables is None:
                raise
            get_logger().debug(f"Retrying review prediction without explicit vars after TypeError: {e}")
            return await self._get_prediction_for_diff(model, patches_diff)

    async def _get_prediction_for_diff(self, model: str, patches_diff: str, variables: dict = None) -> str:
        """
        Generate an AI prediction for the pull request review.

        Args:
            model: A string representing the AI model to be used for the prediction.
            patches_diff: The token-budgeted PR diff to review.

        Returns:
            A string representing the AI prediction for the pull request review.
        """
        variables = copy.deepcopy(variables or self.vars)
        variables["diff"] = patches_diff  # update diff

        environment = Environment(undefined=StrictUndefined)
        system_prompt = environment.from_string(get_settings().pr_review_prompt.system).render(variables)
        user_prompt = environment.from_string(get_settings().pr_review_prompt.user).render(variables)

        response, finish_reason = await self.ai_handler.chat_completion(
            model=model,
            temperature=get_settings().config.temperature,
            system=system_prompt,
            user=user_prompt
        )

        return response

    async def _prepare_prediction_ensemble(self, ensemble: EnsembleConfig) -> None:
        get_logger().info(f"Running ensemble review with models {ensemble.models}, "
                          f"consolidator {ensemble.consolidator}")
        # compute the per-model diffs sequentially before fanning out the AI calls:
        # get_pr_diff mutates token counts on the provider's cached diff files, so
        # it must not run concurrently
        member_diffs = {}
        member_vars = {}
        for model in ensemble.models:
            variables = self._format_repo_context_vars(model)
            token_handler = self._build_token_handler(variables,
                                                      get_settings().pr_review_prompt.system,
                                                      get_settings().pr_review_prompt.user)
            patches_diff = get_pr_diff(self.git_provider,
                                       token_handler,
                                       model,
                                       add_line_numbers_to_hunks=True,
                                       disable_extra_lines=False, )
            if patches_diff:
                member_diffs[model] = patches_diff
                member_vars[model] = variables
            else:
                get_logger().warning(f"Empty diff for PR: {self.pr_url} (ensemble model {model})")

        models_with_diff = [model for model in ensemble.models if model in member_diffs]
        predictions = await gather_ensemble_predictions(
            lambda model: self._get_prediction_for_diff_compat(model, member_diffs[model], member_vars[model]),
            models_with_diff)
        if not predictions:
            get_logger().warning("All ensemble models failed, falling back to the standard review flow")
            await retry_with_fallback_models(self._prepare_prediction, model_type=ModelType.REGULAR)
            return

        self.ensemble_models_used = [model for model, _ in predictions]
        if len(predictions) == 1:
            get_logger().info("Single ensemble prediction available, skipping consolidation")
            self.prediction = predictions[0][1]
            return

        self.ensemble_consolidator = ensemble.consolidator
        try:
            self.prediction = await self._consolidate_predictions(predictions, ensemble.consolidator)
            self.ensemble_consolidated = True
        except Exception as e:
            get_logger().warning(f"Ensemble consolidation with {ensemble.consolidator} failed, "
                                 f"using the review from {predictions[0][0]}", artifact={"error": e})
            self.prediction = predictions[0][1]

    async def _consolidate_predictions(self, predictions: List[Tuple[str, str]],
                                       consolidator: str) -> str:
        model_reviews = ""
        for model, prediction in predictions:
            model_reviews += f"  ## Review from model '{model}': \n======\n{prediction.strip()}\n======\n\n"

        variables = self._format_repo_context_vars(consolidator)
        variables["model_reviews"] = model_reviews
        # the consolidation token handler accounts for the model reviews, so the
        # diff is budgeted to fit alongside them
        token_handler = self._build_token_handler(variables,
                                                  get_settings().pr_review_consolidate_prompt.system,
                                                  get_settings().pr_review_consolidate_prompt.user)
        patches_diff = get_pr_diff(self.git_provider,
                                   token_handler,
                                   consolidator,
                                   add_line_numbers_to_hunks=True,
                                   disable_extra_lines=False, )
        variables["diff"] = patches_diff

        environment = Environment(undefined=StrictUndefined)
        system_prompt = environment.from_string(
            get_settings().pr_review_consolidate_prompt.system).render(variables)
        user_prompt = environment.from_string(
            get_settings().pr_review_consolidate_prompt.user).render(variables)
        response, finish_reason = await self.ai_handler.chat_completion(
            model=consolidator,
            temperature=get_settings().config.temperature,
            system=system_prompt,
            user=user_prompt
        )
        if finish_reason == "length":
            get_logger().warning("Consolidation response was truncated (finish_reason=length) "
                                 f"for consolidator {consolidator}")
        if not response or not response.strip():
            raise Exception("Empty consolidation response")
        return response

    def _prepare_pr_review(self) -> str:
        """
        Prepare the PR review by processing the AI prediction and generating a markdown-formatted text that summarizes
        the feedback.
        """
        first_key = 'review'
        last_key = 'security_concerns'
        data = load_yaml(
            self.prediction.strip(),
            keys_fix_yaml=[
                "ticket_compliance_check",
                "estimated_effort_to_review_[1-5]: ",
                "security_concerns: ",
                "key_issues_to_review: ",
                "relevant_file: ",
                "relevant_line: ",
                "suggestion: "],
            first_key=first_key,
            last_key=last_key)
        github_action_output(data, 'review')

        if 'review' not in data:
            get_logger().exception("Failed to parse review data", artifact={"data": data})
            return ""

        if get_settings().get("repo_context.enabled", False) and self.repo_context_bundle:
            validate_key_issues_to_review(data, self.git_provider.get_diff_files())

        # move data['review'] 'key_issues_to_review' key to the end of the dictionary
        if 'key_issues_to_review' in data['review']:
            key_issues_to_review = data['review'].pop('key_issues_to_review')
            data['review']['key_issues_to_review'] = key_issues_to_review

        incremental_review_markdown_text = None
        # Add incremental review section
        if self.incremental.is_incremental:
            last_commit_url = f"{self.git_provider.get_pr_url()}/commits/" \
                f"{self.git_provider.incremental.first_new_commit_sha}"
            incremental_review_markdown_text = f"Starting from commit {last_commit_url}"

        markdown_text = convert_to_markdown_v2(data, self.git_provider.is_supported("gfm_markdown"),
                                               incremental_review_markdown_text,
                                               git_provider=self.git_provider,
                                               files=self.git_provider.get_diff_files())

        # Add help text if gfm_markdown is supported
        if self.git_provider.is_supported("gfm_markdown") and get_settings().pr_reviewer.enable_help_text:
            markdown_text += "<hr>\n\n<details> <summary><strong>💡 Tool usage guide: </strong></summary><hr> \n\n"
            markdown_text += HelpMessage.get_review_usage_guide()
            markdown_text += "\n</details>\n"

        # Output the relevant configurations if enabled
        if get_settings().get('config', {}).get('output_relevant_configurations', False):
            markdown_text += show_relevant_configurations(relevant_section='pr_reviewer')

        # Add custom labels from the review prediction (effort, security)
        self.set_review_labels(data)

        if markdown_text is None or len(markdown_text) == 0:
            markdown_text = ""

        if markdown_text and self.ensemble_models_used:
            markdown_text += ensemble_footer(self.ensemble_models_used,
                                             self.ensemble_consolidator,
                                             self.ensemble_consolidated)

        return markdown_text

    def _get_user_answers(self) -> Tuple[str, str]:
        """
        Retrieves the question and answer strings from the discussion messages related to a pull request.

        Returns:
            A tuple containing the question and answer strings.
        """
        question_str = ""
        answer_str = ""

        if self.is_answer:
            discussion_messages = self.git_provider.get_issue_comments()

            for message in discussion_messages.reversed:
                if "Questions to better understand the PR:" in message.body:
                    question_str = message.body
                elif '/answer' in message.body:
                    answer_str = message.body

                if answer_str and question_str:
                    break

        return question_str, answer_str

    def _get_previous_review_comment(self):
        """
        Get the previous review comment if it exists.
        """
        try:
            if hasattr(self.git_provider, "get_previous_review"):
                return self.git_provider.get_previous_review(
                    full=not self.incremental.is_incremental,
                    incremental=self.incremental.is_incremental,
                )
        except Exception as e:
            get_logger().exception(f"Failed to get previous review comment, error: {e}")

    def _remove_previous_review_comment(self, comment):
        """
        Remove the previous review comment if it exists.
        """
        try:
            if comment:
                self.git_provider.remove_comment(comment)
        except Exception as e:
            get_logger().exception(f"Failed to remove previous review comment, error: {e}")

    def _can_run_incremental_review(self) -> bool:
        """
        Checks if we can run incremental review according the various configurations and previous review.
        """
        # checking if running is auto mode but there are no new commits
        if self.is_auto and not self.incremental.first_new_commit_sha:
            get_logger().info(f"Incremental review is enabled for {self.pr_url} but there are no new commits")
            return False

        if not hasattr(self.git_provider, "get_incremental_commits"):
            get_logger().info(f"Incremental review is not supported for {get_settings().config.git_provider}")
            return False
        # checking if there are enough commits to start the review
        num_new_commits = len(self.incremental.commits_range)
        num_commits_threshold = get_settings().pr_reviewer.minimal_commits_for_incremental_review
        not_enough_commits = num_new_commits < num_commits_threshold
        # checking if the commits are not too recent to start the review
        recent_commits_threshold = datetime.datetime.now() - datetime.timedelta(
            minutes=get_settings().pr_reviewer.minimal_minutes_for_incremental_review
        )
        last_seen_commit_date = (
            self.incremental.last_seen_commit.commit.author.date if self.incremental.last_seen_commit else None
        )
        all_commits_too_recent = (
            last_seen_commit_date > recent_commits_threshold if self.incremental.last_seen_commit else False
        )
        # check all the thresholds or just one to start the review
        condition = any if get_settings().pr_reviewer.require_all_thresholds_for_incremental_review else all
        if condition((not_enough_commits, all_commits_too_recent)):
            get_logger().info(
                f"Incremental review is enabled for {self.pr_url} but didn't pass the threshold check to run: "
                f"\n* Number of new commits = {num_new_commits} (threshold is {num_commits_threshold})"
                f"\n* Last seen commit date = {last_seen_commit_date} (threshold is {recent_commits_threshold})"
            )
            return False
        return True

    def set_review_labels(self, data):
        if not get_settings().config.publish_output:
            return

        if not get_settings().pr_reviewer.require_estimate_effort_to_review:
            get_settings().pr_reviewer.enable_review_labels_effort = False  # we did not generate this output
        if not get_settings().pr_reviewer.require_security_review:
            get_settings().pr_reviewer.enable_review_labels_security = False  # we did not generate this output

        if (get_settings().pr_reviewer.enable_review_labels_security or
                get_settings().pr_reviewer.enable_review_labels_effort):
            try:
                review_labels = []
                if get_settings().pr_reviewer.enable_review_labels_effort:
                    estimated_effort = data['review']['estimated_effort_to_review_[1-5]']
                    estimated_effort_number = 0
                    if isinstance(estimated_effort, str):
                        try:
                            estimated_effort_number = int(estimated_effort.split(', ')[0])
                        except ValueError:
                            get_logger().warning(f"Invalid estimated_effort value: {estimated_effort}")
                    elif isinstance(estimated_effort, int):
                        estimated_effort_number = estimated_effort
                    else:
                        get_logger().warning(f"Unexpected type for estimated_effort: {type(estimated_effort)}")
                    if 1 <= estimated_effort_number <= 5:  # 1, because ...
                        review_labels.append(f'Review effort {estimated_effort_number}/5')
                if get_settings().pr_reviewer.enable_review_labels_security and get_settings().pr_reviewer.require_security_review:  # noqa: E501
                    security_concerns = data['review']['security_concerns']  # yes, because ...
                    security_concerns_bool = 'yes' in security_concerns.lower() or 'true' in security_concerns.lower()
                    if security_concerns_bool:
                        review_labels.append('Possible security concern')

                current_labels = self.git_provider.get_pr_labels(update=True)
                if not current_labels:
                    current_labels = []
                get_logger().debug(f"Current labels: \n{current_labels}")
                if current_labels:
                    current_labels_filtered = [label for label in current_labels if not label.lower().startswith(
                        'review effort') and not label.lower().startswith('possible security concern')]
                else:
                    current_labels_filtered = []
                new_labels = review_labels + current_labels_filtered
                if (current_labels or review_labels) and sorted(new_labels) != sorted(current_labels):
                    get_logger().info(f"Setting review labels: \n{review_labels + current_labels_filtered}")
                    self.git_provider.publish_labels(new_labels)
                else:
                    get_logger().info(f"Review labels are already set: \n{review_labels + current_labels_filtered}")
            except Exception as e:
                get_logger().error(f"Failed to set review labels, error: {e}")

    def auto_approve_logic(self):
        """
        Auto-approve a pull request if it meets the conditions for auto-approval.
        """
        if get_settings().config.enable_auto_approval:
            is_auto_approved = self.git_provider.auto_approve()
            if is_auto_approved:
                get_logger().info("Auto-approved PR")
                self.git_provider.publish_comment("Auto-approved PR")
        else:
            get_logger().info("Auto-approval option is disabled")
            self.git_provider.publish_comment(
                "Auto-approval option for PR-Agent is disabled. "
                "You can enable it via a [configuration file](https://github.com/Codium-ai/pr-agent/blob/main/docs/REVIEW.md#auto-approval-1)")  # noqa: E501
