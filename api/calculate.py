from typing import Optional, Dict, Any
import asyncio
import json
import re
import hashlib
from fastapi import HTTPException
from pandas import DataFrame, Series
from deepdiff import DeepDiff

from model.data_handler import DataHandler
from solution import factory, factory_2

from api.config import AioWrap, get_resource_path
from api.queries.workbook_queries import workbook_by_id
from api.transform import rehydrate_legacy_json
from api.db.models import Workbook

def to_json(scenario):
    json_data = dict()
    instance_vars = vars(scenario).keys()
    for iv in instance_vars:
        try:
            obj = getattr(scenario, iv)
            if issubclass(type(obj), DataHandler):
                json_data[iv] = obj.to_json()
                for jv in json_data[iv]:
                  if type(json_data[iv][jv]) in [DataFrame, Series]:
                    json_data[iv][jv] = json.loads(json_data[iv][jv].to_json())
        except BaseException:
            json_data[iv] = None
    json_data.pop('tm', None)
    json_data.pop('ae', None)
    return {'name': scenario.name, 'data': json_data}

async def calc(constructor, input, hashed_json_input, technology):
    return [to_json(constructor(input)), hashed_json_input, technology]

async def fetch_data(variation, client) -> [dict, dict, dict]:
  scenario_parent_path = variation['scenario_parent_path']
  reference_parent_path = variation['reference_parent_path']
  scenario_data = await client(scenario_parent_path)
  if not scenario_data:
    raise HTTPException(status_code=400, detail=f"Scenario not found: {scenario_parent_path}")
  reference_data = await client(reference_parent_path)
  if not reference_data:
    raise HTTPException(status_code=400, detail=f"Reference not found: {reference_parent_path}")
  return [variation, scenario_data, reference_data]

def build_json(start_year: int, end_year: int, variation_data: dict, scenario_data: dict, reference_data: dict):
  jsons = list(map(lambda tech: {
    'tech': tech,
    'json': rehydrate_legacy_json(
      start_year,
      end_year,
      tech,
      scenario_data['data'],
      reference_data['data'],
      variation_data)
    }, scenario_data['data']['technologies']))
  return list(filter(lambda json: json['tech'] != 'fossilfuelelectricity', jsons))

async def setup_calculations(jsons, cache):
  tasks = []
  key_list = []
  json_cached_results = []
  constructors = factory_2.all_solutions_scenarios(jsons)

  for constructor in constructors:
    current_json_input: dict = list(filter(lambda json: json['tech'] == constructor, jsons))[0]
    name = current_json_input['json']['name']
    technology = current_json_input['tech']
    copied_json_input = current_json_input.copy()
    # deleting vmas because they're not always serializable (todo?)
    del copied_json_input['json']['vmas']
    hashed_json_input = hashlib.md5(json.dumps(copied_json_input).encode('utf-8')).hexdigest()

    cached_result = await cache.get(hashed_json_input)
    if cached_result is None:
      # Inputs have changed for technology
      tasks.append(calc(constructors[constructor][0], name, hashed_json_input, technology))
    else:
      # Inputs have not changed for technology
      key_list.append([technology, name, hashed_json_input, False])
      json_cached_results.append(json.loads(cached_result))
  return [tasks, key_list, json_cached_results]

async def perform_calculations(tasks, cache, key_list, prev_results, version):
  json_results = []
  if len(tasks) > 0:
    calculated_results = await asyncio.wait(tasks)
    for r in calculated_results[0]:
      [json_result, key_hash, tech] = r._result
      json_results.append(json_result)
      await cache.set(key_hash, json.dumps(json_result))
      if prev_results:
        prev_tech_result = list(filter(lambda result: result['technology_full'] == json_result['name'], prev_results['results']))[0]
        prev_cached_json_result = json.loads(await cache.get(prev_tech_result['hash']))
        # do diff
        delta = DeepDiff(json_result, prev_cached_json_result, ignore_order=True)
        await cache.set(f'delta-{key_hash}', json.dumps(delta))
        key_list.append([tech, json_result['name'], key_hash, True])
      else:
        key_list.append([tech, json_result['name'], key_hash, False])
  return [json_results, key_list]

def build_result_paths(key_list):
  return [{
      'path': get_resource_path('projection', key_hash),
      'hash': key_hash,
      'technology': tech,
      'technology_full': tech_full,
      'delta':  get_resource_path('delta', key_hash) if has_delta else None
    } for [tech, tech_full, key_hash, has_delta] in key_list]

def compound_key(workbook_id: int, workbook_version: int):
  return f'workbook-{workbook_id}-{workbook_version}'

async def get_prev_calc(workbook_id: int, workbook_version: int, cache) -> [int, Dict[Any, Any]]:
  keys = await cache.keys(f'workbook-{workbook_id}-*')
  if len(keys) > 0:
    versions = [int(re.search(r'(\d+)[^-]*$', key.decode("utf-8")).group(0)) for key in keys]
    versions.sort()
    prev_version = versions[-1]
    cache_key = compound_key(workbook_id, prev_version)
    cached_result = await cache.get(cache_key)
    if cached_result is not None:
      return [prev_version, f'workbook-{workbook_id}-{prev_version}', json.loads(cached_result)]
  return [None, None, {}]

async def calculate(workbook_id: int, workbook_version: Optional[int], variation_index: int, client, db, cache):
  workbook: Workbook = workbook_by_id(db, workbook_id)
  if workbook_version is None:
    workbook_version = workbook.version

  cache_key = compound_key(workbook_id, workbook_version)
  cached_result = await cache.get(cache_key)
  if cached_result is not None:
    return json.loads(cached_result)

  [prev_version, prev_key, prev_data] = await get_prev_calc(workbook_id, workbook_version, cache)

  if workbook is None:
    raise HTTPException(status_code=400, detail="Workbook not found")
  result_paths = []
  variation = workbook.variations[variation_index]
  input_data = await fetch_data(variation, client)
  jsons = build_json(workbook.start_year, workbook.end_year, *input_data)
  [tasks, key_list, _] = await setup_calculations(jsons, cache)
  [_, key_list] = await perform_calculations(tasks, cache, key_list, prev_data, workbook_version)
  result_paths += build_result_paths(key_list)
  result = {
    'meta': {
      'previous_run': {
        'version': prev_version,
        'data': get_resource_path('projection_run', prev_key)
      } if prev_version else None,
      'current_run': {
        'version': workbook_version,
        'data': get_resource_path('projection_run', cache_key)
      }
    },
    'results': result_paths
  }
  await cache.set(cache_key, json.dumps(result))

  return result

# async def calculate_diff(workbook_id: int, workbook_version: int, client, db, cache):
#   compound_key = f'{workbook_id}-{workbook_version}'
#   prev_compound_key = f'{workbook_id}-{workbook_version - 1}'

#   prev_cached_result = await cache.get(prev_compound_key)
#   if not prev_cached_result:
#     return calculate(workbook_id, workbook_version, client, db, cache)

#   cached_result = await cache.get(compound_key)
#   if cached_result is not None:
#     return json.loads(cached_result)

#   workbook: Workbook = workbook_by_id(db, workbook_id)
#   if workbook is None:
#     raise HTTPException(status_code=400, detail="Workbook not found")
#   result_paths = []
#   for variation_path in workbook.variations:
#     input_data = await fetch_data(variation_path, client)
#     jsons = build_json(workbook.start_year, workbook.end_year, *input_data)
#     [tasks, key_list, _] = await setup_calculations(jsons, cache)
#     [_, key_list] = await perform_calculations(tasks, cache, key_list)
#     result_paths += build_result_paths(key_list)
#     await cache.set(compound_key, json.dumps(result_paths))

#   return result_paths