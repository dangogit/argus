ALTER TABLE conversation_contexts
  DROP CONSTRAINT IF EXISTS conversation_contexts_context_type_check;

ALTER TABLE conversation_contexts
  ADD CONSTRAINT conversation_contexts_context_type_check
  CHECK (context_type IN ('support_case', 'branch_drift', 'system_health',
                          'interaction'));
