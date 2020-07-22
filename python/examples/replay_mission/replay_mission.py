# Copyright (c) 2020 Boston Dynamics, Inc.  All rights reserved.
#
# Downloading, reproducing, distributing or otherwise using the SDK Software
# is subject to the terms and conditions of the Boston Dynamics Software
# Development Kit License (20191101-BDSDK-SL).

"""
Mission Replay Script.  Command-line utility to replay stored missions, including Autowalk missions.
"""

import os
import sys
import time

from bosdyn.api.graph_nav import map_pb2, nav_pb2
from bosdyn.api.mission import mission_pb2
from bosdyn.api.mission import nodes_pb2
from bosdyn.api import estop_pb2

import bosdyn.client
import bosdyn.client.lease
import bosdyn.client.util

from bosdyn.client.estop import EstopClient
from bosdyn.client.robot_command import RobotCommandBuilder, RobotCommandClient
from bosdyn.client.robot_state import RobotStateClient

import bosdyn.geometry
import bosdyn.mission.client
import bosdyn.util


def main():
    '''Replay stored mission'''

    import argparse

    body_lease = None

    # Configure logging
    bosdyn.client.util.setup_logging()

    # Parse command-line arguments
    parser = argparse.ArgumentParser()

    bosdyn.client.util.add_common_arguments(parser)

    # If the map directory is omitted, we assume that everything the user wants done is encoded in
    # the mission itself.
    parser.add_argument('--map_directory', nargs='?', help='Optional path to map directory')
    parser.add_argument('--mission', dest='mission_file', help='Optional path to mission file')
    parser.add_argument('--timeout', type=float, default=3.0, dest='timeout',
                        help='Mission client timeout (s).')

    group = parser.add_mutually_exclusive_group()

    group.add_argument('--time', type=float, default=0.0, dest='duration',
                       help='Time to repeat mission (sec)')
    group.add_argument('--static', action='store_true', default=False, dest='static_mode',
                       help='Stand, but do not run robot')

    args = parser.parse_args()

    # Use the optional map_directory argument as a proxy for these other tasks we normally do.
    do_map_load = args.map_directory is not None
    fail_on_question = args.map_directory is not None

    if not args.mission_file:
        if not args.map_directory:
            raise Exception('Must specify at least one of map_directory or --mission.')
        args.mission_file = os.path.join(args.map_directory, 'missions', 'autogenerated')

    print('[ REPLAYING MISSION {} : MAP {} : HOSTNAME {} ]'.format(args.mission_file,
                                                                   args.map_directory,
                                                                   args.hostname))

    # Initialize robot object
    robot = init_robot(args.hostname, args.username, args.password, args.app_token)

    # Acquire robot lease
    robot.logger.info('Acquiring lease...')
    lease_client = robot.ensure_client(bosdyn.client.lease.LeaseClient.default_service_name)
    body_lease = lease_client.acquire()
    if body_lease is None:
        raise Exception('Lease not acquired.')
    robot.logger.info('Lease acquired: %s', str(body_lease))

    try:
        with bosdyn.client.lease.LeaseKeepAlive(lease_client):

            # Initialize clients
            robot_state_client, command_client, mission_client, graph_nav_client = init_clients(
                robot, body_lease, args.mission_file, args.map_directory, do_map_load)

            verify_estop(robot)

            # Ensure robot is powered on
            assert ensure_power_on(robot), 'Robot power on failed.'

            # Stand up
            robot.logger.info('Commanding robot to stand...')
            stand_command = RobotCommandBuilder.stand_command()
            command_client.robot_command(stand_command)
            countdown(5)
            robot.logger.info('Robot standing.')

            # Run mission
            if not args.static_mode:
                if args.duration == 0.0:
                    run_mission(robot, mission_client, lease_client, fail_on_question, args.timeout)
                else:
                    repeat_mission(robot, mission_client, lease_client, args.duration,
                                   fail_on_question, args.timeout)

    finally:
        # Ensure robot is powered off
        power_off_success = ensure_power_off(robot)

        # Return lease
        robot.logger.info('Returning lease...')
        lease_client.return_lease(body_lease)


def init_robot(hostname, username, password, token):
    '''Initialize robot object'''

    # Initialize SDK
    sdk = bosdyn.client.create_standard_sdk('MissionReplay', [bosdyn.mission.client.MissionClient])
    sdk.load_app_token(token)

    # Create robot object
    robot = sdk.create_robot(hostname)

    # Authenticate with robot
    robot.authenticate(username, password)

    # Establish time sync with the robot
    robot.time_sync.wait_for_sync()

    return robot


def init_clients(robot, lease, mission_file, map_directory, do_map_load):
    '''Initialize clients'''

    if not os.path.isfile(mission_file):
        robot.logger.fatal('Unable to find mission file: {}.'.format(mission_file))
        sys.exit(1)

    graph_nav_client = None
    if do_map_load:
        if not os.path.isdir(map_directory):
            robot.logger.fatal('Unable to find map directory: {}.'.format(map_directory))
            sys.exit(1)

        # Create graph-nav client
        robot.logger.info('Creating graph-nav client...')
        graph_nav_client = robot.ensure_client(
            bosdyn.client.graph_nav.GraphNavClient.default_service_name)

        # Clear map state and localization
        robot.logger.info('Clearing graph-nav state...')
        graph_nav_client.clear_graph()

        # Upload map to robot
        upload_graph_and_snapshots(robot, graph_nav_client, lease.lease_proto, map_directory)

    # Create mission client
    robot.logger.info('Creating mission client...')
    mission_client = robot.ensure_client(bosdyn.mission.client.MissionClient.default_service_name)

    # Upload mission to robot
    upload_mission(robot, mission_client, mission_file, lease)

    # Create command client
    robot.logger.info('Creating command client...')
    command_client = robot.ensure_client(RobotCommandClient.default_service_name)

    # Create robot state client
    robot.logger.info('Creating robot state client...')
    robot_state_client = robot.ensure_client(RobotStateClient.default_service_name)

    return robot_state_client, command_client, mission_client, graph_nav_client


def countdown(length):
    '''Print sleep countdown'''

    for i in range(length, 0, -1):
        print(i, end=' ', flush=True)
        time.sleep(1)
    print(0)


def upload_graph_and_snapshots(robot, client, lease, path):
    '''Upload the graph and snapshots to the robot'''

    # Load the graph from disk.
    graph_filename = os.path.join(path, 'graph')
    robot.logger.info('Loading graph from ' + graph_filename)

    with open(graph_filename, 'rb') as graph_file:
        data = graph_file.read()
        current_graph = map_pb2.Graph()
        current_graph.ParseFromString(data)
        robot.logger.info('Loaded graph has {} waypoints and {} edges'.format(
            len(current_graph.waypoints), len(current_graph.edges)))

    # Load the waypoint snapshots from disk.
    current_waypoint_snapshots = dict()
    for waypoint in current_graph.waypoints:

        snapshot_filename = os.path.join(path, 'waypoint_snapshots', waypoint.snapshot_id)
        robot.logger.info('Loading waypoint snapshot from ' + snapshot_filename)

        with open(snapshot_filename, 'rb') as snapshot_file:
            waypoint_snapshot = map_pb2.WaypointSnapshot()
            waypoint_snapshot.ParseFromString(snapshot_file.read())
            current_waypoint_snapshots[waypoint_snapshot.id] = waypoint_snapshot

    # Load the edge snapshots from disk.
    current_edge_snapshots = dict()
    for edge in current_graph.edges:

        snapshot_filename = os.path.join(path, 'edge_snapshots', edge.snapshot_id)
        robot.logger.info('Loading edge snapshot from ' + snapshot_filename)

        with open(snapshot_filename, 'rb') as snapshot_file:
            edge_snapshot = map_pb2.EdgeSnapshot()
            edge_snapshot.ParseFromString(snapshot_file.read())
            current_edge_snapshots[edge_snapshot.id] = edge_snapshot

    # Upload the graph to the robot.
    robot.logger.info('Uploading the graph and snapshots to the robot...')
    client.upload_graph(graph=current_graph, lease=lease)
    robot.logger.info('Uploaded graph.')

    # Upload the snapshots to the robot.
    for waypoint_snapshot in current_waypoint_snapshots.values():
        client.upload_waypoint_snapshot(waypoint_snapshot=waypoint_snapshot, lease=lease)
        robot.logger.info('Uploaded {}'.format(waypoint_snapshot.id))

    for edge_snapshot in current_edge_snapshots.values():
        client.upload_edge_snapshot(edge_snapshot=edge_snapshot, lease=lease)
        robot.logger.info('Uploaded {}'.format(edge_snapshot.id))


def upload_mission(robot, client, filename, lease):
    '''Upload the mission to the robot'''

    # Load the mission from disk
    robot.logger.info('Loading mission from ' + filename)

    with open(filename, 'rb') as mission_file:
        data = mission_file.read()
        mission_proto = nodes_pb2.Node()
        mission_proto.ParseFromString(data)

    # Upload the mission to the robot
    robot.logger.info('Uploading the mission to the robot...')
    client.load_mission(mission_proto, leases=[lease])
    robot.logger.info('Uploaded mission to robot.')


def run_mission(robot, mission_client, lease_client, fail_on_question, mission_timeout):
    '''Run mission once'''

    robot.logger.info('Running mission')

    mission_state = mission_client.get_state()

    while mission_state.status in (mission_pb2.State.STATUS_NONE, mission_pb2.State.STATUS_RUNNING):

        # We optionally fail if any questions are triggered. This often indicates a problem in
        # Autowalk missions.
        if mission_state.questions and fail_on_question:
            robot.logger.info('Mission failed by triggering operator question.')
            return False

        body_lease = lease_client.lease_wallet.advance()
        local_pause_time = time.time() + mission_timeout

        mission_client.play_mission(local_pause_time, [body_lease])
        time.sleep(1)

        mission_state = mission_client.get_state()

    return mission_state.status in (mission_pb2.State.STATUS_SUCCESS,
                                    mission_pb2.State.STATUS_PAUSED)


def restart_mission(robot, mission_client, lease_client, mission_timeout):
    '''Restart current mission'''

    robot.logger.info('Restarting mission')

    body_lease = lease_client.lease_wallet.advance()
    local_pause_time = time.time() + mission_timeout

    status = mission_client.restart_mission(local_pause_time, [body_lease])
    time.sleep(1)

    return status == mission_pb2.State.STATUS_SUCCESS


def repeat_mission(robot, mission_client, lease_client, total_time, fail_on_question, timeout):
    '''Repeat mission for period of time'''

    robot.logger.info('Repeating mission for {} seconds.'.format(total_time))

    # Run first mission
    start_time = time.time()
    mission_success = run_mission(robot, mission_client, lease_client, fail_on_question, timeout)
    elapsed_time = time.time() - start_time
    robot.logger.info('Elapsed time = {} (out of {})'.format(elapsed_time, total_time))

    if not mission_success:
        robot.logger.info('Mission failed.')
        return False

    # Repeat mission until total time has expired
    while elapsed_time < total_time:
        restart_mission(robot, mission_client, lease_client, mission_timeout=3)
        mission_success = run_mission(robot, mission_client, lease_client, fail_on_question,
                                      timeout)

        elapsed_time = time.time() - start_time
        robot.logger.info('Elapsed time = {} (out of {})'.format(elapsed_time, total_time))

        if not mission_success:
            robot.logger.info('Mission failed.')
            break

    return mission_success


def ensure_power_off(robot):
    '''Ensure that robot is powered off'''

    if robot.is_powered_on():
        robot.power_off(cut_immediately=False, timeout_sec=20)

    if robot.is_powered_on():
        robot.logger.error('Error powering off robot.')
        return False

    robot.logger.info('Robot safely powered off.')
    return True


def ensure_power_on(robot):
    '''Ensure that robot is powered on'''

    if robot.is_powered_on():
        return True

    robot.logger.info('Powering on robot...')
    robot.power_on(timeout_sec=20)

    if robot.is_powered_on():
        robot.logger.info('Robot powered on.')
        return True

    robot.logger.error('Error powering on robot.')
    return False

def verify_estop(robot):
    """Verify the robot is not estopped"""
    client = robot.ensure_client(EstopClient.default_service_name)
    if client.get_status().stop_level != estop_pb2.ESTOP_LEVEL_NONE:
        error_message = "Robot is estopped. Please use an external E-Stop client, such as the" \
        " estop SDK example, to configure E-Stop."
        robot.logger.error(error_message)
        raise Exception(error_message)


if __name__ == '__main__':
    main()
