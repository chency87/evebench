DEFAULT_PROMPT = """You are an expert in SQL. Your task is to generate a counterexample (as a small test database, where q1 and q2 output different results) for 2 SQL queries.
Given the following two SQL queries Q1 and Q2
Q1:{q1}
Q2:{q2}

And the following database schema:
{schema}
Q1 and Q2 are not semantically equivalent. Please analyze their logic and provide a counterexample (as a test database) to distinguish them. Make sure Q1 and Q2 will return different results, and the counterexample does not violate constraints of the schema.

Response format (strict JSON):
{
  "explanation": "<short explanation on why Q1 and Q2 are not equivalent>",
  "counterexample_dsp": "<short description of the test data and which conditions trigger the difference>",
  "counterexample": "<executable SQL statements (only INSERTs) that create the test data under the given schema and constraints>"
}"""
