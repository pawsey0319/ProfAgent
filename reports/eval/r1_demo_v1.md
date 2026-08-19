# ProfAgent R1 Demo Evaluation

- Status: **PASS**
- Generated at: `2026-08-19T13:51:08+08:00`
- Command: `conda run -n torch128 python -m profagent.eval`
- Dataset: `fixtures_v1.0` / `eval_fixtures_v1.0_30`
- Eval SHA-256: `a534e04346a60b837528248e38891d9e17a1024895f36aea9433e209409bff66`
- External provider calls: `0`
- Main 30-case input: `structured_scene_fields` (`event_horizon`, `intent` supplied)
- Urgency metric definition: `horizon_to_urgency_mapping_accuracy` (not query NLP accuracy)
- Query-only parser assurance: `21` cases
- No-deadline audit assurance: `3` cases
- Query-derived explicit-gap Catalog calls: `e009,e010,e011,e012,e013`
- Legacy `allowed_catalog` semantics: non-authoritative, non-exhaustive `legacy_generator_candidate_witness`; overlap is diagnostic only and does not gate release
- Legacy generator candidate overlap (all cases): `5/12` (overlap_ratio=`0.416667`)

## Metrics

| Metric | Numerator | Denominator | Value | Threshold | Status | Failed cases |
|---|---:|---:|---:|---|---|---|
| urgency_accuracy | 30 | 30 | 1.0 ratio | >= 0.95 | PASS | - |
| high_urgency_shopping_gate_accuracy | 10 | 10 | 1.0 ratio | == 1.0 | PASS | - |
| high_urgency_catalog_calls | 0 | 10 | 0 invocations | == 0 | PASS | - |
| item_hallucinations | 0 | 515 | 0 violations | == 0 | PASS | - |
| hard_constraint_violations | 0 | 423 | 0 violations | == 0 | PASS | - |
| slot_completeness | 74 | 74 | 1.0 ratio | >= 0.95 | PASS | - |

## Acceptance criteria matrix

| AC | Status | Evidence |
|---|---|---|
| AC-01 | PASS | structured 30-case gate aggregate AND exact single mixed-emotion interview Given |
| AC-03 | PASS | same-session two-request unknown-deadline workflow with Catalog trace |
| AC-04 | PASS | propose-confirm no_high_heels memory, implicit reuse and independent recall oracle |
| AC-05 | PASS | e019/e020/e021 owned exact STATUS_* recall filters, ranker absence and independent output hard oracle |
| AC-06 | PASS | 30-case direction contract AND deterministic one-combo sparse interview wardrobe |
| AC-07 | PASS | feedback propose-confirm-apply-delete lifecycle AND causal structured-similar shoe rank demotion |
| AC-10 | PASS | normal garment/Mock-product grounding oracle AND adversarial unknown/cross-owner garment rejection |
| AC-11 | PASS | 30-case upper gate with actual search invocation spy AND forced secondary Catalog guard |
| AC-13 | PASS | strict Vision fail-closed matrix AND exact interview v1-adjust-v2-rescore-Final chain |
| AC-14 | PASS | in-process verified-visual reject/replay/version inheritance workflow |
| AC-16 | PASS | independent closed-term scans of qualitative and MockTransport numeric user-visible scorecard text |
| AC-17 | PASS | qualitative/no-score satisfaction=1 Final AND actual exact-78 numeric Final with post-final locks |

## Executed assurance checks

| Check | Status | Evidence |
|---|---|---|
| QUERY-ONLY-SCENE | PASS | query-only deadline/season/unknown parser subset; no horizon/intent labels supplied; checks=query_only_horizon, query_only_urgency, query_only_intent, query_only_shopping_gate, unknown_to_high_fail_closed |
| AUDIT-NO-DEADLINE | PASS | query-only no-deadline wardrobe/catalog audits fail closed to unknown/high with actual and traced Catalog zero; checks=audit_query_only_unknown, audit_unknown_to_high, audit_scene_and_ui_shopping_false, audit_actual_catalog_zero, audit_trace_catalog_zero |
| AC-03 | PASS | same-session two-request unknown-deadline workflow with Catalog trace; checks=first_unknown_asks_one_question, second_unknown_does_not_repeat, both_high_shopping_false_ui_false, both_catalog_zero |
| AC-04 | PASS | propose-confirm no_high_heels memory, implicit reuse and independent recall oracle; checks=memory_propose_without_commit, memory_confirm_commit, new_query_omits_preference, confirmed_memory_hit_without_reasking, known_heel_filtered, unknown_heel_evidence_filtered, ranker_lists_exclude_unsafe_shoes, outfits_and_alternatives_exclude_unsafe_shoes, evidence_safe_shoe_remains |
| AC-05 | PASS | e019/e020/e021 owned exact STATUS_* recall filters, ranker absence and independent output hard oracle; checks=availability_fixture_status_matches, status_reason_present_in_trace_filters, status_items_absent_from_rule_bm25_dense_rrf, independent_hard_oracle_zero |
| AC-06 | PASS | 30-case direction contract AND deterministic one-combo sparse interview wardrobe; checks=at_most_three_directions, unordered_item_sets_distinct, exactly_one_primary_when_nonempty, reason_risk_alternatives_present, less_than_three_has_explicit_gap, honest_gap_case_observed, exactly_one_complete_interview_direction, explicit_honest_gap, all_ids_current_owner_grounded, independent_hard_oracle_zero, taboo_not_relaxed, taboo_candidate_filtered_before_rankers, taboo_candidate_absent_from_outfits_and_alternatives, counterfactual_second_combo_proven, high_shopping_ui_false, catalog_actual_and_trace_zero |
| AC-07 | PASS | feedback propose-confirm-apply-delete lifecycle AND causal structured-similar shoe rank demotion; checks=like_session_only, propose_without_commit, before_confirm_not_applied, confirm_applies_only_to_relevant_scene, unrelated_scene_not_targeted, delete_restores_eligibility, propose_without_commit, independent_metadata_similarity_3_of_5, before_confirm_no_policy, confirm_excludes_exact_target, same_universe_fixed_penalty_and_risk_rank_never_improves, same_universe_safe_score_unchanged, end_to_end_similar_risk_scores_decrease, end_to_end_safe_relative_margin_improves, unrelated_scene_not_affected, trace_policy_counts_without_private_ids, delete_restores_preconfirm_ranks_and_scores |
| AC-10 | PASS | normal garment/Mock-product grounding oracle AND adversarial unknown/cross-owner garment rejection; checks=normal_garment_and_catalog_output_hallucinations_zero, unknown_id_rejected, cross_owner_id_rejected, non_whitelist_reason_count_two, illegal_ids_not_reflected |
| AC-11 | PASS | 30-case upper gate with actual search invocation spy AND forced secondary Catalog guard; checks=upper_shopping_false_ui_false, upper_trace_attempted_false_call_zero, upper_actual_catalog_invocations_zero, upper_attempted_false_call_zero, secondary_forced_call_zero, secondary_explicit_block_reason, normal_and_forced_suggestions_empty |
| AC-14 | PASS | in-process verified-visual reject/replay/version inheritance workflow; checks=at_most_two_adjustments, canonical_trouser_cuff_single, no_ankle_rejection_boundary_preserved, accept_current_state_closed_alternative, reject_creates_decision_not_fake_version, decided_token_replay_blocked, canonical_not_reproposed, future_version_inherits_rejection, decision_replayable_in_chain, user_modified_not_partial |
| AC-13 | PASS | strict Vision fail-closed matrix AND exact interview v1-adjust-v2-rescore-Final chain; checks=limited_asset_all_null, exact_grok_4_5_full_region_numeric_success, server_owned_evidence_text, model_mismatch_all_null, malformed_all_null, free_text_all_null, slot_mismatch_all_null, extra_field_all_null, partial_all_null, http_error_all_null, interview_reliable_modern_context, owned_sufficient_image_before_v1, v1_numeric_keep_point_one_to_two_adjustments, adjustments_bind_expected_dimensions_and_visual_evidence, accepted_adjustment_and_adjusted_image_create_v2, v1_immutable_v2_parent_index_correct, v2_rescore_has_nonzero_parent_comparison, chain_traceable_and_comparable, satisfied_final_preserves_v2_score, post_final_score_adjust_create_blocked, owners_ids_and_traces_grounded |
| AC-16 | PASS | independent closed-term scans of qualitative and MockTransport numeric user-visible scorecard text; checks=exact_six_dimensions, no_asset_all_scores_null, no_visual_adjustment_without_evidence, outfit_only_subject_boundary, keep_point_present, qualitative_visible_text_closed_scan, numeric_visible_text_closed_scan |
| AC-17 | PASS | qualitative/no-score satisfaction=1 Final AND actual exact-78 numeric Final with post-final locks; checks=satisfaction_one_finalizes, advice_stopped, post_final_adjustment_rejected, independent_six_dimension_exact_78_precondition, actual_numeric_scorecard_exact_78, final_scorecard_id_and_total_preserved, satisfaction_one_finalizes, advice_stopped, post_final_score_adjust_create_blocked |
| SAFE-RAIN | PASS | rain outer/top/bag orchestration plus empty-gap direct Catalog guard; checks=rain_requires_outer, all_unverified_outers_filtered_before_rule_bm25_dense_rrf, outfits_zero_with_explicit_gap, low_outer_top_bag_catalog_items_zero, empty_gap_direct_catalog_items_zero, high_catalog_invocations_zero |
| SAFE-CPA-PRIVACY | PASS | in-process CPA method spy for e028/e029/e030, email, phone and vent; checks=e028_e029_e030_provider_zero, email_provider_zero, phone_provider_zero, explicit_vent_provider_zero, blocked_trace_attempted_false_reason_code, ordinary_query_provider_one |
| OBS-05 | PASS | deterministic LLM/Dense/Catalog/Vision four-leg degradation matrix; checks=llm_failure_attempted, llm_rule_fallback_legal_recommendation, dense_forced_failure, dense_rule_bm25_rrf_legal_recommendation, catalog_forced_failure_actual_invocation, catalog_no_products_wardrobe_or_safe_gap_continues, vision_forced_failure, vision_qualitative_all_null_no_adjustment |
| RESPONSE-VALIDATION-ALIGNMENT | PASS | service OutfitValidation booleans compared with independent hard/slot/grounding oracle; checks=all_reported_validation_flags_match_independent_oracle |
| CATALOG-ORACLE | PASS | query-derived explicit-gap call requirement plus independent full-inventory eligibility, actual/Trace invocation, filtered-reason coverage, fixture/actual-return grounding, forbidden truth and no-gap Catalog zero; allowed_catalog is a non-authoritative legacy generator witness; checks=catalog_call_requirement_derived_from_query_text, e009_e013_explicit_gap_requires_actual_and_trace_call, independent_full_inventory_eligibility_oracle, eligible_nonempty_requires_suggestions_subset_eligible, eligible_empty_requires_safe_empty_suggestions, catalog_trace_covers_every_ineligible_item_with_auditable_reason, all_nonmandatory_cases_actual_and_trace_catalog_zero, catalog_fixture_and_actual_return_grounding, catalog_independent_constraints, catalog_forbidden_eval_truth, allowed_catalog_legacy_generator_candidate_witness_non_gating, catalog_inventory_conditioned_presence_or_safe_absence |
| AC-01 | PASS | structured 30-case gate aggregate AND exact single mixed-emotion interview Given; checks=structured_horizon_to_urgency_threshold, high_shopping_false_catalog_zero, direction_primary_reason_risk_alternatives, controlled_supportive_first_line, single_exact_given, today_high_interview_reliable_modern, supportive_first_line, one_to_three_grounded_formal_directions, exactly_one_primary, reason_risk_alternatives, shopping_ui_false, actual_and_trace_catalog_zero |

## Privacy and reproducibility

The report contains case IDs and aggregate outcomes only. It does not contain full queries, image bytes/base64, authorization headers, or API keys. The fixture metric phase disables CPA text and live Vision. AC assurance uses only in-process injected transports and makes zero external provider calls.
