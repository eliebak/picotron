#VERBOSE=0 torchrun --nproc_per_node 4 --master_addr localhost --master_port 25500 train.py --pp_size 2 --dp_size 2
import os
import torch.nn.functional as F
import torch, torch.distributed as dist
from torch.optim import AdamW
from transformers import AutoConfig, AutoModelForCausalLM

import argparse

import parallel_context as pc
from utils import set_all_seed, display_parallelism_grid
from parallel_context import setup_parallel_context
from pipeline_parallel import train_step_pipeline_1f1b, train_step_pipeline_afab, PipelineParallel
from data_parallel import DataParallel
from dataset import MicroBatchDataLoader

def train_step(model, data_loader, device):
    total_loss = 0.0

    for _ in range(data_loader.num_local_micro_batches):
        batch = next(iter(data_loader))
        
        input_ids = batch["input_ids"].to(device)
        position_ids = batch["position_index"].to(device)
        target_ids = batch["target_ids"].to(device)

        outputs = model(input_ids=input_ids, position_ids=position_ids)
        logits = outputs.logits

        # Use your suggested cross_entropy calculation
        loss = F.cross_entropy(logits.transpose(1, 2), target_ids, reduction='mean')

        loss.backward()

        total_loss += loss.item()

    avg_loss = total_loss / data_loader.num_local_micro_batches
    return avg_loss

if __name__ == "__main__":
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--tp_size", type=int, default=1)
    parser.add_argument("--pp_size", type=int, default=1)
    parser.add_argument("--dp_size", type=int, default=1)
    
    args = parser.parse_args()
    
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    local_rank, world_size = int(os.environ["LOCAL_RANK"]), int(os.environ["WORLD_SIZE"])
    host, port = os.environ["MASTER_ADDR"], int(os.environ["MASTER_PORT"])

    SEQ_LEN, GLOBAL_BATCH_SIZE, MICRO_BATCH_SIZE, LEARNING_RATE, NUM_SAMPLES, MAX_TOKENS = 10, 6, 2, 1e-4, 20, 1800
        
    dist.init_process_group(rank=local_rank, world_size=world_size, backend="nccl", init_method=f"tcp://{host}:{port}")
    torch.cuda.set_device(local_rank)
    device = torch.device("cuda", local_rank)
    setup_parallel_context(tp_size=args.tp_size, pp_size=args.pp_size, dp_size=args.dp_size)

    if pc.parallel_context.global_rank == local_rank:
        display_parallelism_grid()

    set_all_seed(seed=42)
    model_name = "HuggingFaceTB/SmolLM-360M-Instruct"
    config = AutoConfig.from_pretrained(model_name)
    model = AutoModelForCausalLM.from_pretrained(model_name, config=config).to(device)

    if pc.parallel_context.pp_world_size > 1:
        model = PipelineParallel(model, config).to(device)
    
    if pc.parallel_context.dp_world_size > 1:
        model = DataParallel(model, config).to(device)

    model.train()
    
    data_loader = MicroBatchDataLoader(GLOBAL_BATCH_SIZE, MICRO_BATCH_SIZE, SEQ_LEN, "roneneldan/TinyStories", model_name, num_samples=NUM_SAMPLES)
    tensor_shapes = (SEQ_LEN, data_loader.micro_batch_size, config.hidden_size)
    optimizer = AdamW(model.parameters(), lr=LEARNING_RATE)
    
    trained_tokens, step = 0, 0
    tokens_per_step = data_loader.num_global_micro_batches * data_loader.micro_batch_size * SEQ_LEN

    dist.barrier()

    #TODO: find a way to setup reference model training
    #TODO: Add activation checkpointing
    #TODO: add gradient accumulation
    
    while trained_tokens < MAX_TOKENS:        
        data_loader.set_epoch(step)

        optimizer.zero_grad()
        
        if pc.parallel_context.pp_world_size > 1:
            loss = train_step_pipeline_afab(model, data_loader, tensor_shapes, device)
        else:
            loss = train_step(model, data_loader, device)

        if pc.parallel_context.dp_world_size > 1:
            # Average gradient across DP ranks
            model.all_reduce_gradients()

        optimizer.step()
        trained_tokens += tokens_per_step
        step += 1
        
        #NOTE(fmom): change later to log on rank 0 (g00) everytime ?
        if pc.parallel_context.pp_is_last_stage and pc.parallel_context.global_rank == pc.parallel_context.tp_first_rank and pc.parallel_context.global_rank == pc.parallel_context.dp_first_rank:
            print(f"[rank {pc.parallel_context.global_rank}] Step: {step}, Loss: {loss:.4f}, Tokens: {trained_tokens}/{MAX_TOKENS}")
            
    dist.destroy_process_group()