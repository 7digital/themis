from flask import Flask, render_template, jsonify, send_from_directory, request
import util.monitoring
import util.aws_common
import util.aws_pricing
import util.common
import os
import re
import json
import time
import threading
import traceback
from flask_swagger import swagger
from util.aws_common import INSTANCE_GROUP_TYPE_TASK

root_path = os.path.dirname(os.path.realpath(__file__))
web_dir = root_path + '/web/'

app = Flask('app', template_folder='web')
app.root_path = root_path

# time to sleep between loops
LOOP_SLEEP_TIMEOUT_SECS = 3 * 60

# config file location
CONFIG_FILE_LOCATION = './autoscaling.config.json'
CLUSTERS_FILE_LOCATION = './autoscaling.clusters.json'

# global configuration values
KEY_LOOP_INTERVAL_SECS = 'loop_interval_secs'
KEY_UPSCALE_ITERATIONS = 'upscale_trigger_iterations'
KEY_DOWNSCALE_ITERATIONS = 'downscale_trigger_iterations'
KEY_UPSCALE_EXPR = 'upscale_expr'
KEY_DOWNSCALE_EXPR = 'downscale_expr'
KEY_AUTOSCALING_CLUSTERS = 'autoscaling_clusters'
KEY_PREFERRED_UPSCALE_INSTANCE_MARKET = 'preferred_upscale_instance_market'
KEY_MONITORING_INTERVAL_SECS = 'monitoring_interval_secs'

# instance market constants
MARKET_ON_DEMAND = "ON_DEMAND"
MARKET_SPOT = "SPOT"

KEY = 'key'
VAL = 'value'
DESC = 'description'
DEFAULT_APP_CONFIG = [
	{KEY: KEY_AUTOSCALING_CLUSTERS, VAL: '', DESC: 'Comma-separated list of cluster IDs to auto-scale'},
	{KEY: KEY_DOWNSCALE_EXPR, VAL: "1 if (tasknodes.running and tasknodes.active and tasknodes.count.nodes >= 2 and tasknodes.average.cpu < 0.5 and tasknodes.average.mem < 0.9) else 0", DESC: 'Trigger cluster downscaling by the number of nodes this expression evaluates to'},
	{KEY: KEY_UPSCALE_EXPR, VAL: "3 if (tasknodes.running and tasknodes.active and tasknodes.count.nodes < 15 and (tasknodes.average.cpu > 0.7 or tasknodes.average.mem > 0.95)) else 0", DESC: "Trigger cluster upscaling by the number of nodes this expression evaluates to"},
	{KEY: KEY_UPSCALE_ITERATIONS, VAL: "1", DESC: "Number of consecutive times %s needs to evaluate to true before upscaling"},
	{KEY: KEY_LOOP_INTERVAL_SECS, VAL: LOOP_SLEEP_TIMEOUT_SECS, DESC: 'Loop interval seconds'},
	{KEY: KEY_PREFERRED_UPSCALE_INSTANCE_MARKET, VAL: MARKET_SPOT, DESC: 'Whether to preferably increase the pool of SPOT instances or ON_DEMAND instances (if both exist in the cluster)'},
	{KEY: KEY_MONITORING_INTERVAL_SECS, VAL: 60 * 10, DESC: 'Time period (seconds) of historical monitoring data to consider for scaling decisions'}
]

CLUSTER_LIST = util.common.load_json_file(CLUSTERS_FILE_LOCATION, [])
CLUSTERS = {}
for val in CLUSTER_LIST:
	CLUSTERS[val['id']] = val

@app.route('/swagger.json')
def spec():
	swag = swagger(app)
	swag['info']['version'] = "1.0"
	swag['info']['title'] = "Cluster Management API"
	return jsonify(swag)

@app.route('/state/<cluster_id>')
def get_state(cluster_id):
	""" Get cluster state
		---
		operationId: 'getState'
		parameters:
			- name: cluster_id
			  in: path
	"""
	monitoring_interval_secs = int(get_config_value(KEY_MONITORING_INTERVAL_SECS))
	info = util.monitoring.collect_info(CLUSTERS[cluster_id], monitoring_interval_secs)
	return jsonify(info)

@app.route('/history/<cluster_id>')
def get_history(cluster_id):
	""" Get cluster state history
		---
		operationId: 'getHistory'
		parameters:
			- name: 'cluster_id'
			  in: path
	"""
	info = util.monitoring.history_get(cluster_id, 100)
	util.common.remove_NaN(info)
	return jsonify(results=info)

@app.route('/clusters')
def get_clusters():
	""" Get list of clusters
		---
		operationId: 'getClusters'
	"""
	return jsonify(results=CLUSTER_LIST)

@app.route('/config', methods=['GET'])
def get_config():
	""" Get configuration
		---
		operationId: 'getConfig'
	"""
	appConfig = read_config()
	return jsonify({'config': appConfig})

@app.route('/config', methods=['POST'])
def set_config():
	""" Set configuration
		---
		operationId: 'setConfig'
		parameters:
			- name: 'config'
			  in: body
	"""
	newConfig = json.loads(request.data)
	write_config(newConfig)
	appConfig = read_config()
	return jsonify({'config': appConfig})

@app.route('/restart', methods=['POST'])
def restart_node():
	""" Restart a cluster node
		---
		operationId: 'restartNode'
		parameters:
			- name: 'request'
			  in: body
	"""
	data = json.loads(request.data)
	cluster_id = data['cluster_id'];
	node_host = data['node_host'];
	for c_id, details in CLUSTERS.iteritems():
		if c_id == cluster_id:
			cluster_ip = details['ip']
			tasknodes_group = util.aws_common.get_instance_group_for_node(cluster_id, node_host)
			if tasknodes_group:
				terminate_node(cluster_ip, node_host, tasknodes_group)
				return jsonify({'result': 'SUCCESS'});
	return jsonify({'result': 'Invalid cluster ID provided'});

@app.route('/')
def hello():
	return render_template('index.html')

@app.route('/<path:path>')
def send_static(path):
	return send_from_directory(web_dir + '/', path)

@app.route('/costs', methods=['POST'])
def get_costs():
	""" Get summary of cluster costs and cost savings
		---
		operationId: 'getCosts'
		parameters:
			- name: 'request'
			  in: body
	"""
	data = json.loads(request.data)
	cluster_id = data['cluster_id']
	num_datapoints = data['num_datapoints'] if 'num_datapoints' in data else 300
	baseline_nodes = data['baseline_nodes'] if 'baseline_nodes' in data else 15
	info = util.monitoring.history_get(cluster_id, num_datapoints)
	util.common.remove_NaN(info)
	result = util.aws_pricing.get_cluster_savings(info, baseline_nodes)
	return jsonify(results=result)

def sort_nodes_by_load(nodes, weight_mem=1, weight_cpu=2, desc=False):
	return sorted(nodes, reverse=desc, key=lambda node: (\
			float((node['load']['mem'] if 'mem' in node['load'] else 0)*weight_mem) + \
			float((node['load']['cpu'] if 'cpu' in node['load'] else 0)*weight_cpu)))


#------------------#
# HELPER FUNCTIONS #
#------------------#

def read_config():
	appConfig = util.common.load_json_file(CONFIG_FILE_LOCATION)
	if appConfig:
		return appConfig['config']
	write_config(DEFAULT_APP_CONFIG)
	return DEFAULT_APP_CONFIG

def write_config(config):
	configToStore = {'config': config}
	util.common.save_json_file(CONFIG_FILE_LOCATION, configToStore)
	return config

def get_config_value(key, config=None):
	if not config:
		config = read_config()
	for c in config:
		if c[KEY] == key:
			return c[VAL]
	return None

def get_autoscaling_clusters():
	return re.split(r'\s*,\s*', get_config_value(KEY_AUTOSCALING_CLUSTERS))

def get_termination_candidates(info, ignore_preferred=False):
	candidates = []
	for key, details in info['nodes'].iteritems():
		if details['type'] == util.aws_common.INSTANCE_GROUP_TYPE_TASK:
			if 'queries' not in details:
				details['queries'] = 0
			if details['queries'] == 0:
				group_details = util.aws_common.get_instance_group_details(info['cluster_id'], details['gid'])
				preferred = get_config_value(KEY_PREFERRED_UPSCALE_INSTANCE_MARKET)
				if ignore_preferred or group_details['market'] == preferred:
					candidates.append(details)
	return candidates

def get_nodes_to_terminate(info):
	expr = get_config_value(KEY_DOWNSCALE_EXPR)
	num_downsize = util.monitoring.execute_dsl_string(expr, info)
	print("num_downsize: %s" % num_downsize)
	if not isinstance(num_downsize, int) or num_downsize <= 0:
		return []

	candidates = get_termination_candidates(info)

	if len(candidates) <= 0:
		candidates = get_termination_candidates(info, ignore_preferred=True)

	candidates = sort_nodes_by_load(candidates, desc=False)

	result = []
	if candidates:
		for cand in candidates:
			ip = util.aws_common.hostname_to_ip(cand['host'])
			instance_info = {
				'iid': cand['iid'],
				'cid': cand['cid'],
				'gid': cand['gid'],
				'ip': ip
			}
			result.append(instance_info)
			if len(result) >= num_downsize:
				return result
	return result

def get_nodes_to_add(info):
	expr = get_config_value(KEY_UPSCALE_EXPR)
	num_upsize = util.monitoring.execute_dsl_string(expr, info)
	print("num_upsize: %s" % num_upsize)
	if isinstance(num_upsize, int) and num_upsize > 0:
		return ['TODO' for i in range(0,num_upsize)]
	return []

def terminate_node(cluster_ip, node_ip, tasknodes_group):
	print("Sending shutdown signal to task node with IP '%s'" % node_ip)
	util.aws_common.set_presto_node_state(cluster_ip, node_ip, util.aws_common.PRESTO_STATE_SHUTTING_DOWN)

def spawn_nodes(cluster_ip, tasknodes_group, current_num_nodes, nodes_to_add=1):
	print("Adding new task node to cluster '%s'" % cluster_ip)
	util.aws_common.spawn_task_node(tasknodes_group, current_num_nodes, nodes_to_add)

def select_tasknode_group(tasknodes_groups):
	if len(tasknodes_groups) <= 0:
		raise Exception("Empty list of task node instance groups for scaling: %s" % tasknodes_groups)
	if len(tasknodes_groups) == 1:
		return tasknodes_groups[0]
	preferred = get_config_value(KEY_PREFERRED_UPSCALE_INSTANCE_MARKET)
	for group in tasknodes_groups:
		if group['market'] == preferred:
			return group
	raise Exception("Could not select task node instance group for preferred market '%s': %s" % (preferred, tasknodes_groups))


def tick():
	print("Running next loop iteration")
	monitoring_interval_secs = int(get_config_value(KEY_MONITORING_INTERVAL_SECS))
	for cluster_id, details in CLUSTERS.iteritems():
		cluster_ip = details['ip']
		info = util.monitoring.collect_info(details, monitoring_interval_secs)
		action = 'N/A'
		# Make sure we are only resizing Presto clusters atm
		if details['type'] == 'Presto':
			# Make sure we don't change clusters that are not configured
			if cluster_id in get_autoscaling_clusters():
				nodes_to_terminate = get_nodes_to_terminate(info)
				if len(nodes_to_terminate) > 0:
					for node in nodes_to_terminate:
						terminate_node(cluster_ip, node['ip'], node['gid'])
					action = 'DOWNSCALE(-%s)' % len(nodes_to_terminate)
				else:
					nodes_to_add = get_nodes_to_add(info)
					if len(nodes_to_add) > 0:
						tasknodes_groups = util.aws_common.get_instance_groups_tasknodes(cluster_id)
						tasknodes_group = select_tasknode_group(tasknodes_groups)['id']
						current_num_nodes = len([n for key,n in info['nodes'].iteritems() if n['gid'] == tasknodes_group])
						spawn_nodes(cluster_ip, tasknodes_group, current_num_nodes, len(nodes_to_add))
						action = 'UPSCALE(+%s)' % len(nodes_to_add)
					else:
						action = 'NOTHING'
				# clean up and terminate instances whose nodes are already in inactive state
				util.aws_common.terminate_inactive_nodes(cluster_ip, info['nodes'])
		# store the state for future reference
		util.monitoring.history_add(cluster_id, info, action)

def loop():
	while True:
		try:
			tick()
		except Exception, e:
			print("WARN: Exception in main loop: %s" % (e))
			traceback.print_exc()
		time.sleep(LOOP_SLEEP_TIMEOUT_SECS)

def serve(port):
	app.run(port=int(port), debug=True, threaded=True, host='0.0.0.0')