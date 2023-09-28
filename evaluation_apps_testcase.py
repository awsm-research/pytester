import ast
import os
import torch
import logging
import pickle
import datasets
import pandas as pd
from tqdm import tqdm
from torch.utils.data import DataLoader
from transformers import AutoModelForSeq2SeqLM, AutoModelForCausalLM, AutoTokenizer, PLBartForCausalLM, \
                        DataCollatorForSeq2Seq, DataCollatorForLanguageModeling
from handlers.code_processing import evaluation
from handlers.testing_util_v2 import functional_evaluation, split_test_cases, filtered_functional_evaluation

#########################
######## config #########
model_name = 'codet5'
model_dir  = 'save/APPS/codet5-testcase-prompt_text-v2/checkpoint-10000/hyperparameter-2-lre6/PPO_multi_hcec-reward24_hyper2-2/step-better-114-4.872499942779541'
test_dataset_path = 'dataset/APPS_new/apps_test_executable.csv'
input_column = 'prompt_testcase' 
output_column = 'output_testcase'
pred_mode = 'multi' # multi or single

output_dir = model_dir
output_suffix = '_beam300'

batch_size = 1
beam_size = 5
max_new_tokens = 300 # overwrite max_length
max_length = 512

gpu_num = '1'

#########################
base_model = {
    'pycoder': AutoModelForCausalLM,
    'codegpt': AutoModelForCausalLM,
    'transformers': AutoModelForCausalLM,
    'gpt2': AutoModelForCausalLM,
    'codet5': AutoModelForSeq2SeqLM,
    'plbart': PLBartForCausalLM,
}
os.environ["CUDA_VISIBLE_DEVICES"] = gpu_num
os.environ['TRANSFORMERS_NO_ADVISORY_WARNINGS'] = 'true'
os.environ["TOKENIZERS_PARALLELISM"] = "true"

# Set logging
if not os.path.exists(output_dir):
    os.makedirs(output_dir)
log_file = output_dir + '/prediction.log'
logger = logging.getLogger(__name__)
logging.basicConfig(filename=log_file, filemode='a', format='%(asctime)s - %(levelname)s - %(name)s - %(message)s', datefmt='%m/%d/%Y %H:%M:%S', level=logging.INFO)
logger.addHandler(logging.StreamHandler())
logger.info(f"Is CUDA available: {torch.cuda.is_available()}")
logger.info(f"CUDA device: {torch.cuda.get_device_name(torch.cuda.current_device())}")
logger.info(f'''evaluated_model_dir: {model_dir},
            tokenizer_dir: {model_dir},
            test_data_dir: {test_dataset_path},
            input_column: {input_column},
            output_column: {output_column},
            output_dir: {output_dir},
            output_suffix: {output_suffix},
            -- Generator params --
            batch_size: {batch_size},
            beam_size: {beam_size},
            max_new_tokens: {max_new_tokens},
            max_length: {max_length},
            pred_mode: {pred_mode}
            ''')
device = "cuda" if torch.cuda.is_available() else "cpu"
stop_gen_word = '\nassert' if pred_mode == 'single' else '</s>'

# Load the CSV test dataset 
test_df = pd.read_csv(test_dataset_path)
logger.info(f'# test data: {len(test_df)}')

# Load the trained CodeT5 model and initialize the tokenizer
model = base_model[model_name].from_pretrained(model_dir).to(device)
tokenizer = AutoTokenizer.from_pretrained(model_dir, truncation_side='left', 
                                          padding_side='left' if model_name in ['codegpt','pycoder','gpt2','transformers', 'plbart'] else 'right', 
                                          do_lower_case=False)
if model_name == 'gpt2':
    tokenizer.pad_token = tokenizer.eos_token
logger.info('loaded model and tokenizer sucessfully.')

# Tokenize the inputs for the test dataset
test_inputs_text = list(test_df[input_column])
test_outputs_text = list(test_df[output_column])
fn_names=list(test_df['fn_name'])
prompt_testcases=list(test_df['prompt_testcase'])
prompt_codes=list(test_df['prompt_code'])
output_testcases=list(test_df['output_testcase'])
output_solutions=list(test_df['output_solution'])
test_inputs = tokenizer(test_inputs_text, padding=True, truncation=True, max_length=max_length)

# Convert the tokenized inputs into a PyTorch dataset
test_dataset = datasets.Dataset.from_dict({
    "input_ids": test_inputs["input_ids"],
    "attention_mask": test_inputs["attention_mask"],
})

# Create PyTorch dataloaders for the test dataset
test_loader = DataLoader(test_dataset, batch_size=batch_size, collate_fn=DataCollatorForSeq2Seq(tokenizer) if 'codet5' in model_name else DataCollatorForLanguageModeling(tokenizer, mlm=False))
logger.info('loaded dataset sucessfully.')

# Generate predictions on the test dataset
generation_kwargs = {
    "min_new_tokens": 5,
    # "temperature": 0.7,
    # # "top_k": 0.0,
    # "top_p": 0.7,
    # "do_sample": True,
    "max_new_tokens": max_new_tokens,
    "pad_token_id": tokenizer.eos_token_id,
    # "eos_token_id": tokenizer('\n').input_ids[1:],
    "num_return_sequences": beam_size,
    "num_beams": beam_size,
    # "no_repeat_ngram_size": 3,
}
logger.info(f'generation_kwargs: {generation_kwargs}')
predictions = []
raw_ouputs = []
for batch in tqdm(test_loader):
    with torch.no_grad():
        input_ids = batch["input_ids"].to(device)
        attention_mask = batch["attention_mask"].to(device)
        if model_name in ['codegpt','pycoder','gpt2','transformers','plbart'] and max_new_tokens is not None:
            outputs = model.generate(input_ids=input_ids, attention_mask=attention_mask, 
                                 pad_token_id=tokenizer.eos_token_id,
                                 max_new_tokens=max_new_tokens, 
                                 num_return_sequences=beam_size, num_beams=beam_size, early_stopping=True, max_time=30)
            # trim output to only generated tokens
            outputs = outputs[:, input_ids.shape[-1]:]
        else:
            outputs = model.generate(input_ids=input_ids, attention_mask=attention_mask, **generation_kwargs)
        raw_ouputs.append([outputs[i:i+beam_size] for i in range(0, len(outputs), beam_size)])
        decoded_outputs = tokenizer.batch_decode(outputs, skip_special_tokens=True, clean_up_tokenization_spaces=False)
        decoded_outputs = [pred[:pred.find(stop_gen_word)] if stop_gen_word in pred else pred for pred in decoded_outputs]
        decoded_outputs = [decoded_outputs[i:i+beam_size] for i in range(0, len(decoded_outputs), beam_size)]
        predictions.extend(decoded_outputs)

# Save predictions to file: txt and pickle
first_predictions = [pred[0] for pred in predictions]
with open(output_dir + f'/prediction-{beam_size}{output_suffix}.pkl', 'wb') as f:
    pickle.dump(predictions, f)
with open(output_dir + f'/predictions-{beam_size}{output_suffix}.txt', 'w') as f:
    for data in first_predictions:
        f.write(data + '\n')
# with open(output_dir + f'/raw_prediction-{beam_size}{output_suffix}.pkl', 'wb') as f:
#     pickle.dump(raw_ouputs, f)

# filter assertions syntax
filtered_predictions = []
for i in range(len(predictions)):
    temp = []
    for p in predictions[i]:
        checked_assertions = split_test_cases(p, fn_names[i], filter_syntax=True)
        temp.append("\n".join(checked_assertions)[7:].replace('test_call_solution(', 'call_solution('))
    filtered_predictions.append(temp)
filtered_first_predictions = [pred[0] for pred in filtered_predictions]
with open(output_dir + f'/filtered_prediction-{beam_size}{output_suffix}.pkl', 'wb') as f:
    pickle.dump(filtered_predictions, f)

del tokenizer
del model
del input_ids
del attention_mask
torch.cuda.empty_cache()

# Evaluate the predictions against the expected outputs
logger.info(f'*None-filtered assertions syntax results: {pred_mode}*')
em, es, mrr, parsable = evaluation(predictions, test_outputs_text, test_inputs_text, processed=False)
logger.info(f'Exact Match: {em}%')
logger.info(f'Edit Similarity: {es}%')
logger.info(f'MRR: {mrr}%')
logger.info('-----AST Parsable Rate-----')
for k in parsable:
    logger.info(f'{k}: {parsable[k]}%')
logger.info(f'*Filtered assertions syntax results: {pred_mode}*')
em, es, mrr, parsable = evaluation(filtered_predictions, test_outputs_text, test_inputs_text, processed=False)
logger.info(f'Exact Match: {em}%')
logger.info(f'Edit Similarity: {es}%')
logger.info(f'MRR: {mrr}%')
logger.info('-----AST Parsable Rate-----')
for k in parsable:
    logger.info(f'{k}: {parsable[k]}%')

logger.info('-----Functional Rate-----')
logger.info(f'*None-filtered assertions syntax results: {pred_mode}*')
functional, coverage, mutation = functional_evaluation(prompts=prompt_codes,
                                   solutions=output_solutions,
                                   testcases=first_predictions,
                                   fn_names=fn_names,
                                   debug=False,
                                   on_guard=True,
                                   on_codet_result=False,
                                   add_test_call_solution=True,
                                   filter_syntax=False,
                                   unique_name=output_dir[-3:]+output_suffix)
with open(output_dir + f'/evaluation_perfect{output_suffix}.pkl', 'wb') as f:
    pickle.dump([functional, coverage, mutation], f)
for k in functional:
    logger.info(f'{k}: {functional[k]}%')
    print(f'{k}: {functional[k]}%')
logger.info(f'coverage: {sum(coverage) / len(coverage)}%')
print(f'coverage: {sum(coverage) / len(coverage)}%')
logger.info(f'Mutation score: {sum(mutation) / len(mutation)}%')
print(f'Mutation score: {sum(mutation) / len(mutation)}%')
# logger.info(f'*Filtered assertions syntax results: {pred_mode}*')
# functional, coverage, mutation = functional_evaluation(prompts=prompt_codes,
#                                    solutions=output_solutions,
#                                    testcases=first_predictions,
#                                    fn_names=fn_names,
#                                    debug=False,
#                                    on_guard=True,
#                                    on_codet_result=False,
#                                    add_test_call_solution=True,
#                                    filter_syntax=True,
#                                    unique_name=output_dir[-3:]+output_suffix)
# with open(output_dir + f'/evaluation_syntax_filter{output_suffix}.pkl', 'wb') as f:
#     pickle.dump([functional, coverage, mutation], f)
# logger.info('Functional Rate')
# for k in functional:
#     logger.info(f'{k}: {functional[k]}%')
#     print(f'{k}: {functional[k]}%')
# logger.info(f'coverage: {sum(coverage) / len(coverage)}%')
# print(f'coverage: {sum(coverage) / len(coverage)}%')
# logger.info(f'Mutation score: {sum(mutation) / len(mutation)}%')
# print(f'Mutation score: {sum(mutation) / len(mutation)}%')

# logger.info(f'*Filtered assertions error results: {pred_mode}*')
# syntax, functional, coverage, mutation = filtered_functional_evaluation(prompts=prompt_codes,
#                                    solutions=output_solutions,
#                                    testcases=first_predictions,
#                                    fn_names=fn_names,
#                                    debug=False,
#                                    on_guard=True,
#                                    on_codet_result=False,
#                                    add_test_call_solution=True,
#                                    filter_syntax=True,
#                                    unique_name=output_dir[-3:]+output_suffix)
# with open(output_dir + f'/evaluation_exec_filter{output_suffix}.pkl', 'wb') as f:
#     pickle.dump([syntax, functional, coverage, mutation], f)
# logger.info('Compilable Rate')
# for k in syntax:
#     logger.info(f'{k}: {syntax[k]}%')
#     print(f'{k}: {syntax[k]}%')
# logger.info('Functional Rate')
# for k in functional:
#     logger.info(f'{k}: {functional[k]}%')
#     print(f'{k}: {functional[k]}%')
# logger.info(f'Coverage: {sum(coverage) / len(coverage)}%')
# print(f'Coverage: {sum(coverage) / len(coverage)}%')
# logger.info(f'Mutation score: {sum(mutation) / len(mutation)}%')
# print(f'Mutation score: {sum(mutation) / len(mutation)}%')

logger.info('finish program.')