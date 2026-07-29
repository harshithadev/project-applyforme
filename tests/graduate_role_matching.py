from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    sys.path.insert(0, str(REPO_ROOT))

    from job_agent import job_sources, jobs
    from job_agent.role_matching import experience_requirements

    settings = {
        "career_stage_mode": "graduate",
        "target_role_families": (
            "product,project_program,agile_delivery,consulting,"
            "change_transformation,strategy_operations"
        ),
        "graduate_max_required_experience_years": "3",
        "graduate_include_internships": "false",
        "additional_title_aliases": "",
        "excluded_title_terms": (
            "senior, principal, director, head of, vice president, "
            "vp, chief, lead"
        ),
        "locations": "remote",
        "target_companies": "",
        "posted_within_days": "0",
    }

    def evaluate(
        title: str,
        description: str,
        overrides: dict[str, str] | None = None,
        *,
        location: str = "Remote",
        workplace_type: str = "remote",
    ) -> jobs.MatchDecision:
        posting = job_sources.JobPosting(
            title=title,
            company="ExampleCo",
            url=f"https://example.test/jobs/{title.casefold().replace(' ', '-')}",
            description=description,
            location=location,
            workplace_type=workplace_type,
        )
        return jobs.evaluate_posting(posting, {**settings, **(overrides or {})})

    product = evaluate(
        "Associate Product Manager",
        "An early career role for recent graduates. 0-2 years of experience. "
        "Support the roadmap, user research, backlog, and stakeholder management.",
    )
    assert product.accepted and product.score >= 85, product
    assert any("Product management" in reason for reason in product.reasons)

    renamed_change_role = evaluate(
        "Graduate Transformation Partner",
        "A graduate programme focused on change impact, adoption, training, "
        "and stakeholder analysis.",
    )
    assert renamed_change_role.accepted, renamed_change_role
    assert any("Change and transformation" in reason for reason in renamed_change_role.reasons)

    unrelated = evaluate(
        "Software Engineer",
        "Work in agile project delivery with consulting teams, change management, "
        "stakeholders, and product roadmaps.",
    )
    assert not unrelated.accepted and "title did not match" in unrelated.rejection

    senior = evaluate(
        "Senior Product Manager",
        "Own the roadmap. No prior experience is required.",
    )
    assert not senior.accepted and "seniority" in senior.rejection

    allowed_senior = evaluate(
        "Senior Product Analyst",
        "Support product roadmaps and customer discovery.",
        {"excluded_title_terms": ""},
    )
    assert allowed_senior.accepted, allowed_senior

    overqualified = evaluate(
        "Project Coordinator",
        "This role requires 4+ years of project delivery experience.",
    )
    assert (
        not overqualified.accepted
        and "requires at least 4 years" in overqualified.rejection
    )

    three_year_requirement = evaluate(
        "Project Coordinator",
        "This role requires 3+ years of project delivery experience.",
    )
    assert three_year_requirement.accepted, three_year_requirement
    assert any(
        "Required experience: 3 years" in reason
        for reason in three_year_requirement.reasons
    )

    preferred_only = evaluate(
        "Project Coordinator",
        "Experience is welcome. 4 years of project delivery experience is preferred.",
    )
    assert preferred_only.accepted, preferred_only

    required_then_preferred = experience_requirements(
        "At least 4 years of project delivery experience required. "
        "A professional certification is preferred."
    )
    assert required_then_preferred["required_years"] == 4, required_then_preferred
    assert required_then_preferred["preferred_years"] is None, required_then_preferred

    possessive_experience = evaluate(
        "Business Analyst",
        "Requires 2 years' client delivery experience and strong problem solving.",
    )
    assert possessive_experience.accepted, possessive_experience
    assert any("Required experience: 2 years" in reason for reason in possessive_experience.reasons)

    internship = evaluate(
        "Product Analyst Intern",
        "Support customer discovery and roadmap analysis.",
    )
    included_internship = evaluate(
        "Product Analyst Intern",
        "Support customer discovery and roadmap analysis.",
        {"graduate_include_internships": "true"},
    )
    assert not internship.accepted and "internships are excluded" in internship.rejection
    assert included_internship.accepted, included_internship

    product_manager_intern = evaluate(
        "Product Manager Intern",
        "Support product strategy and delivery.",
        {"graduate_include_internships": "true"},
    )
    assert product_manager_intern.accepted, product_manager_intern

    quantitative_strategy_intern = evaluate(
        "Quantitative Development & Strategy Intern",
        "Develop quantitative trading systems.",
        {"graduate_include_internships": "true"},
    )
    assert not quantitative_strategy_intern.accepted, quantitative_strategy_intern

    custom_title = evaluate(
        "Transformation Partner",
        "Support organizational change and adoption.",
        {
            "target_role_families": "product",
            "additional_title_aliases": "transformation partner",
        },
    )
    assert custom_title.accepted, custom_title
    assert any("Custom title" in reason for reason in custom_title.reasons)

    selected_families = evaluate(
        "Graduate Consultant",
        "Entry-level client delivery and problem solving.",
        {"target_role_families": "product,project_program"},
    )
    assert not selected_families.accepted

    open_mode = evaluate(
        "Implementation Specialist",
        "Configure customer systems.",
        {
            "career_stage_mode": "open",
            "role_keywords": "implementation specialist",
        },
    )
    assert open_mode.accepted, open_mode

    us_role = evaluate(
        "Associate Product Manager",
        "OPT candidates are welcome and H-1B sponsorship is available in the future.",
        {
            "locations": "United States",
            "work_authorization_mode": "cpt_opt_future_sponsorship",
            "sponsorship_unknown_handling": "review",
        },
        location="New York, NY",
        workplace_type="hybrid",
    )
    assert us_role.accepted, us_role
    assert any("CPT/OPT mentioned" in reason for reason in us_role.reasons)

    worldwide_remote = evaluate(
        "Project Coordinator",
        "Entry-level remote role. Visa sponsorship is available.",
        {
            "locations": "United States",
            "work_authorization_mode": "cpt_opt_future_sponsorship",
        },
        location="Worldwide",
        workplace_type="remote",
    )
    assert worldwide_remote.accepted, worldwide_remote

    non_us_role = evaluate(
        "Junior Consultant",
        "Entry-level client delivery role with visa sponsorship available.",
        {"locations": "United States"},
        location="Munich",
        workplace_type="onsite",
    )
    assert not non_us_role.accepted and "location" in non_us_role.rejection

    no_sponsorship = evaluate(
        "Consulting Analyst",
        "Candidates must be authorized to work without requiring sponsorship now or in the future.",
        {
            "locations": "United States",
            "work_authorization_mode": "cpt_opt_future_sponsorship",
        },
        location="Chicago, IL",
        workplace_type="hybrid",
    )
    assert (
        not no_sponsorship.accepted
        and "excludes future sponsorship" in no_sponsorship.rejection
    )

    unknown_sponsorship = evaluate(
        "Strategy Analyst",
        "Entry-level market analysis and business strategy role.",
        {
            "locations": "United States",
            "work_authorization_mode": "cpt_opt_future_sponsorship",
            "sponsorship_unknown_handling": "review",
        },
        location="Boston, MA",
        workplace_type="onsite",
    )
    assert unknown_sponsorship.accepted, unknown_sponsorship
    assert any("needs verification" in reason for reason in unknown_sponsorship.reasons)

    print("graduate role matching ok")


if __name__ == "__main__":
    main()
