import solution_user_agent  # noqa: F401 - registers the AWS Solutions user-agent hook; import first
import json
import statistics
from collections import defaultdict

from joblib import Parallel, delayed
from joblib_progress import joblib_progress

from agent_client import AgentClient
from evaluation import TESTS_JSON, CREDENTIALS_JSON, FAILED_THRESHOLD
from llm_as_a_judge import Agent_Evaluator


CREDENTIALS = json.loads(open(CREDENTIALS_JSON).read())
PARALLEL_JOBS = 5


def run_test(test):
    agent = AgentClient(CREDENTIALS['username'],CREDENTIALS['password'])
    evaluator = Agent_Evaluator()

    response = agent.get_response(test['question'], test['aoi'],
                                  debug_img_folder=f"{test['use-case'].replace(' ', '_')}_{test['id']}")
    evaluator = Agent_Evaluator()

    eval_response = evaluator.evaluate(test['question'], test['answer'], response.response_text)

    return {
        "test": test,
        "response": response.response_text,
        "score": eval_response['score'],
        "rationale": eval_response['rationale']
    }


def load_tests(use_case=None):
    tests = []
    for test in json.load(open(TESTS_JSON)):
        if not test['answer'] or not test['aoi'] or not test['question'] :
            continue

        if use_case and test['use-case'] != use_case:
            continue

        tests.append(test)
    return tests


def benchmark(tests):
    with joblib_progress("Agent Benchmark", total=len(tests)):
        processed = Parallel(n_jobs=PARALLEL_JOBS)(delayed(run_test)(test) for test in tests)
    
    results_by_type = defaultdict(lambda: {
        'scores': [],
        'failed': []
    })
    for result in processed:
        use_case = result['test']['use-case']
        score = result['score']
        results_by_type[use_case]['scores'].append(score)
        if score < FAILED_THRESHOLD:
            results_by_type[use_case]['failed'].append(result['test']['id'])

    macro_scores = []
    for use_case, result in results_by_type.items():
        results = results_by_type[use_case]
        print(f"# {use_case} [{len(results['scores'])} tests]")
        score = statistics.mean(results['scores'])
        macro_scores.append(score)
        print(f" - Score: {score:.0%}")
        if results['failed']:
            print(f" - Failed Tests: {results['failed']}")

    macro_score = statistics.mean(macro_scores)
    print(f"Overall Macro Score: {macro_score:.0%}")


if __name__ == "__main__":
    from argparse import ArgumentParser

    parser = ArgumentParser()
    parser.add_argument("--use_case", default=None)
    args = parser.parse_args()

    tests = load_tests(args.use_case)

    benchmark(tests)
