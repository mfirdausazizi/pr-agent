from jinja2 import Environment, StrictUndefined

from pr_agent.config_loader import get_settings


def review_consolidate_vars():
    # mirrors PRReviewer.__init__ self.vars plus the consolidation-only variables
    return {
        "title": "test title", "branch": "main", "description": "desc",
        "language": "Python", "diff": "the-diff", "num_pr_files": 1,
        "num_max_findings": 3, "require_score": False, "require_tests": True,
        "require_estimate_effort_to_review": True,
        "require_estimate_contribution_time_cost": False,
        "require_can_be_split_review": False, "require_security_review": True,
        "require_todo_scan": False, "question_str": "", "answer_str": "",
        "extra_instructions": "", "commit_messages_str": "", "custom_labels": "",
        "enable_custom_labels": False, "is_ai_metadata": False,
        "related_tickets": [], "duplicate_prompt_examples": False,
        "date": "2026-06-12",
        "repo_context": "",
        "repo_context_status": "unavailable",
        "consolidation_verification_context": "",
        "model_reviews": "## Review from model 'model-a':\nreview-a-yaml",
    }


def test_review_consolidate_prompt_is_registered_and_renders():
    settings = get_settings()
    system_template = settings.get("pr_review_consolidate_prompt.system")
    user_template = settings.get("pr_review_consolidate_prompt.user")
    assert system_template
    assert user_template

    environment = Environment(undefined=StrictUndefined)
    variables = review_consolidate_vars()
    rendered_system = environment.from_string(system_template).render(variables)
    rendered_user = environment.from_string(user_template).render(variables)

    assert "$PRReview" in rendered_system
    assert "key_issues_to_review" in rendered_system
    assert "review-a-yaml" in rendered_user
    assert "the-diff" in rendered_user
    assert "Related Repository Context" not in rendered_user

    variables["repo_context"] = "caller context"
    variables["repo_context_status"] = "ok"
    variables["consolidation_verification_context"] = "test context"
    rendered_user = environment.from_string(user_template).render(variables)
    assert "Related Repository Context" in rendered_user
    assert "caller context" in rendered_user
    assert "Consolidation Verification Context" in rendered_user
    assert "test context" in rendered_user

    # Re-render with duplicate_prompt_examples and related_tickets to test new blocks
    variables["duplicate_prompt_examples"] = True
    variables["related_tickets"] = [{"ticket_url": "u", "title": "t", "labels": "l", "body": "b", "requirements": "r"}]
    rendered = environment.from_string(user_template).render(variables)
    assert "(replace '...'" in rendered
    assert "Ticket Requirements:" in rendered


def test_review_prompt_renders_strictly_with_repo_context_vars():
    settings = get_settings()
    environment = Environment(undefined=StrictUndefined)
    variables = review_consolidate_vars()
    variables.pop("model_reviews")

    rendered_without_context = environment.from_string(settings.pr_review_prompt.user).render(variables)
    assert "Related Repository Context" not in rendered_without_context

    variables["repo_context"] = "repository evidence"
    variables["repo_context_status"] = "ok"
    rendered_with_context = environment.from_string(settings.pr_review_prompt.user).render(variables)
    assert "Related Repository Context" in rendered_with_context
    assert "repository evidence" in rendered_with_context


def test_reflect_consolidate_prompt_is_registered_and_renders():
    settings = get_settings()
    system_template = settings.get("pr_code_suggestions_reflect_consolidate_prompt.system")
    user_template = settings.get("pr_code_suggestions_reflect_consolidate_prompt.user")
    assert system_template
    assert user_template

    # mirrors the variables dict built by self_reflect_on_suggestions
    variables = {"suggestion_list": [], "suggestion_str": "suggestion 1: {...}",
                 "diff": "the-diff", "num_code_suggestions": 1,
                 "prev_suggestions_str": "", "is_ai_metadata": False,
                 "duplicate_prompt_examples": False}
    environment = Environment(undefined=StrictUndefined)
    rendered_system = environment.from_string(system_template).render(variables)
    rendered_user = environment.from_string(user_template).render(variables)

    assert "duplicate" in rendered_system.lower()
    assert "source_model" in rendered_system
    assert "suggestion 1" in rendered_user
    assert "the-diff" in rendered_user
