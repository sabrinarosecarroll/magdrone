#!/usr/bin/python

import rospy as rp
import threading
import math
import time

from dronekit import connect, VehicleMode, LocationGlobal, LocationGlobalRelative
from pymavlink import mavutil

from sensor_msgs.msg import Joy
from geometry_msgs.msg import Twist, PoseStamped, TwistStamped, Vector3Stamped

def to_rpy(qw, qx, qy, qz):
    r = math.atan2(2 * (qw*qx + qy*qz), 1 - 2*(qx*qx + qy*qy))

    sinP = 2 * (qw*qy - qz*qx)
    if abs(sinP) > 1:
        p = math.copysign(1.0, sinP) * math.pi / 2
    else:
        p = math.asin(sinP)

    y = math.atan2(2 * (qw*qz + qx*qy), 1 - 2*(qy*qy + qz*qz))

    return [math.degrees(r), math.degrees(p), math.degrees(y)]

def to_quaternion(roll=0.0, pitch=0.0, yaw=0.0):
    """
    Convert degrees to quaternions
    """
    t0 = math.cos(math.radians(yaw * 0.5))
    t1 = math.sin(math.radians(yaw * 0.5))
    t2 = math.cos(math.radians(roll * 0.5))
    t3 = math.sin(math.radians(roll * 0.5))
    t4 = math.cos(math.radians(pitch * 0.5))
    t5 = math.sin(math.radians(pitch * 0.5))

    w = t0 * t2 * t4 + t1 * t3 * t5
    x = t0 * t3 * t4 - t1 * t2 * t5
    y = t0 * t2 * t5 + t1 * t3 * t4
    z = t1 * t2 * t4 - t0 * t3 * t5

    return [w, x, y, z]

def opti_to_drone(x_error, y_error, yaw_angle):
    x_error_d = (x_error * math.cos(math.radians(yaw_angle))) - (y_error * math.sin(math.radians(yaw_angle)))
    y_error_d = (x_error * math.sin(math.radians(yaw_angle))) + (y_error * math.cos(math.radians(yaw_angle)))
    return [x_error_d, y_error_d]

class magdroneControlNode():

    def __init__(self):
        rp.init_node("magdrone_deploy")

        # Connect to the Vehicle
        rp.loginfo('Connecting to Vehicle')
        self.vehicle = connect('/dev/serial0', wait_ready=True, baud=57600)

        # Set up Subscribers
        self.pose_sub = rp.Subscriber(
            "/opti_state/pose", PoseStamped, self.pose_callback, queue_size=1)
        self.rate_sub = rp.Subscriber(
            "/opti_state/rates", TwistStamped, self.rate_callback, queue_size=1)
        self.opti_sub = rp.Subscriber(
            "/vrpn_client_node/UAV/pose", PoseStamped, self.opti_callback, queue_size=1)
        self.joy_sub = rp.Subscriber(
            "/joy", Joy, self.joy_callback, queue_size=1)

        # Set up Publishers
        self.setpoint_pub = rp.Publisher("/setpoint", Vector3Stamped, queue_size=1)
        self.command_pub = rp.Publisher("/commands", TwistStamped, queue_size=1)

        # Set up Controllers
        self.kp_z = 0.45
        self.kd_z = 0.3
        self.kp_y = 10.5
        self.kd_y = 8.5
        self.kp_x = 10.5
        self.kd_x = 8.5
        self.kp_w = 0.15

        self.z_error = 0.0
        self.y_error = 0.0
        self.x_error = 0.0
        self.w_error = 0.0
        self.z_error_d = 0.0
        self.y_error_d = 0.0
        self.x_error_d = 0.0

        # Variables
        self.lastOnline = 0
        self.cmds = None
        self.on_mission = False
        self.mission_id = 1
        self.state_id = 0
        self.arm = 0
        self.magnet_engaged = False
        self.docked = True
        self.magnet_button = 0

        # Desired positions for misions 1 and 3
        self.struct_x = 0.1
        self.struct_y = 0.06
        self.struct_z = 1.98 # Offset is reduced because of the addition of the sensor package
        # Mission 1. Incremental altitudes
        # The last one should be unreachable
        self.desired_positions_m1 = [-1.3, -1.2, -1.1, -1.0, -0.9, -0.8, -0.7, -0.6, -0.5, -0.4, -0.3, -0.2, 0.1, -0.5]
        # Mission 2
        self.desired_positions_m2 = [[self.struct_x, self.struct_y, self.struct_z - 0.2],
                                     [self.struct_x, self.struct_y, self.struct_z + 0.1],
                                     [self.struct_x, self.struct_y, self.struct_z - 0.3],
                                     [self.struct_x, self.struct_y, self.struct_z - 0.5],
                                     [0.0, -1.8, 1.0],
                                     [0.0, -1.8, 0.25]]

        # Make a global variable for the current yaw position to be used for pose and rate transormations
        self.yaw_position = 0.0

        # Create thread for publisher
        self.rate = 30
        t = threading.Thread(target=self.send_commands)
        t.start()

        rp.spin()

    def clipCommand(self, cmd, upperBound, lowerBound):
        if cmd < lowerBound:
            cmd = lowerBound
        elif cmd > upperBound:
            cmd = upperBound

        return cmd

    def arm_and_takeoff_nogps(self, aTargetAltitude=-1.0):
        """
        Arms vehicle and fly to aTargetAltitude without GPS data.
        """

        ##### CONSTANTS #####
        DEFAULT_TAKEOFF_THRUST = 0.55
        SMOOTH_TAKEOFF_THRUST = 0.52

        rp.loginfo("Basic pre-arm checks")
        # Don't let the user try to arm until autopilot is ready
        # If you need to disable the arming check,
        # just comment it with your own responsibility.
        while not self.vehicle.is_armable:
            rp.loginfo(" Waiting for vehicle to initialise...")
            time.sleep(1)

        rp.loginfo("Arming motors")
        #  GUIDED_NOGPS mode is recommended by DroneKit
        self.vehicle.mode = VehicleMode("GUIDED_NOGPS")
        self.vehicle.armed = True

        while not self.vehicle.armed:
            rp.loginfo(" Waiting for arming...")
            self.vehicle.armed = True
            time.sleep(1)

        rp.loginfo('Armed')

        if aTargetAltitude > 0:
            rp.loginfo("Taking off!")

            thrust = DEFAULT_TAKEOFF_THRUST
            while True:
                current_altitude = self.vehicle.location.global_relative_frame.alt
                rp.loginfo(" Altitude: %f  Desired: %f" %
                      (current_altitude, aTargetAltitude))
                # Trigger just below target alt.
                if current_altitude >= aTargetAltitude*0.95:
                    rp.loginfo("Reached target altitude")
                    break
                elif current_altitude >= aTargetAltitude*0.6:
                    thrust = SMOOTH_TAKEOFF_THRUST
                self.set_attitude(thrust=thrust)
                time.sleep(0.2)

    def send_attitude_target(self, roll_angle=0.0, pitch_angle=0.0,
                             yaw_angle=None, yaw_rate=0.0, use_yaw_rate=False,
                             thrust=0.5):
        """
        use_yaw_rate: the yaw can be controlled using yaw_angle OR yaw_rate.
                      When one is used, the other is ignored by Ardupilot.
        thrust: 0 <= thrust <= 1, as a fraction of maximum vertical thrust.
                Note that as of Copter 3.5, thrust = 0.5 triggers a special case in
                the code for maintaining current altitude.
        """
        if yaw_angle is None:
            # this value may be unused by the vehicle, depending on use_yaw_rate
            yaw_angle = self.vehicle.attitude.yaw

        # Thrust >  0.5: Ascend
        # Thrust == 0.5: Hold the altitude
        # Thrust <  0.5: Descend
        msg = self.vehicle.message_factory.set_attitude_target_encode(
            0,  # time_boot_ms
            1,  # Target system
            1,  # Target component
            0b00000000 if use_yaw_rate else 0b00000100,
            to_quaternion(roll_angle, pitch_angle, yaw_angle),  # Quaternion
            0,  # Body roll rate in radian
            0,  # Body pitch rate in radian
            math.radians(yaw_rate),  # Body yaw rate in radian/second
            thrust  # Thrust
        )
        self.vehicle.send_mavlink(msg)

    def set_attitude(self, roll_angle=0.0, pitch_angle=0.0,
                     yaw_angle=None, yaw_rate=0.0, use_yaw_rate=False,
                     thrust=0, duration=0.05):
        """
        Note that from AC3.3 the message should be re-sent more often than every
        second, as an ATTITUDE_TARGET order has a timeout of 1s.
        In AC3.2.1 and earlier the specified attitude persists until it is canceled.
        The code below should work on either version.
        Sending the message multiple times is the recommended way.
        """
        self.send_attitude_target(roll_angle, pitch_angle,
                                  yaw_angle, yaw_rate, use_yaw_rate,
                                  thrust)
        start = time.time()
        while time.time() - start < duration:
            self.send_attitude_target(roll_angle, pitch_angle,
                                      yaw_angle, yaw_rate, use_yaw_rate,
                                      thrust)
            time.sleep(duration)
        # Reset attitude, or it will persist for 1s more due to the timeout
        self.send_attitude_target(0, 0, 0, 0, True, thrust)

    '''
        Callbacks
    '''

    def pose_callback(self, data):
        # Get current pose wrt Optitrack
        qw = data.pose.orientation.w
        qx = data.pose.orientation.x
        qy = data.pose.orientation.y
        qz = data.pose.orientation.z
        orientation = to_rpy(qw, qx, qy, qz)

        # Drone body system is Front-Left-Down
        w_drone = -orientation[2]
        x_drone = data.pose.position.x
        y_drone = data.pose.position.y
        z_drone = data.pose.position.z

        # Update yaw
        self.yaw_position = w_drone

        # Get desired position from State Machine
        des_position = self.stateMachine(x_drone, y_drone, z_drone)

        # Publish setpoint
        setpoint = Vector3Stamped()
        setpoint.header.stamp = rp.Time.now()
        setpoint.vector.x = des_position[0]
        setpoint.vector.y = des_position[1]
        setpoint.vector.z = des_position[2]
        self.setpoint_pub.publish(setpoint)

        # Calculate error
        dx = des_position[0] - x_drone
        dy = des_position[1] - y_drone
        dz = des_position[2] - z_drone
        dw = 90.0 - w_drone

        # Translate error from optitrack frame to drone body frame
        drone_error = opti_to_drone(dx, dy, w_drone)

        self.x_error = drone_error[0]
        self.y_error = drone_error[1]
        self.z_error = dz

        if dw > 180.0:
            self.w_error = dw - 360.0
        else:
            self.w_error = dw

    def rate_callback(self, data):
        # Translate error rate from optitrack frame to drone body frame
        drone_error_rate = opti_to_drone(-data.twist.linear.x, -data.twist.linear.y, self.yaw_position)

        # Update rate errors
        self.x_error_d =  drone_error_rate[0]
        self.y_error_d =  drone_error_rate[1]
        self.z_error_d = -data.twist.linear.z

    def opti_callback(self, data):
        self.lastOnline = time.time()

    def joy_callback(self, data):
        # Empty Command
        self.cmds = Twist()

        # Joystick Controls
        self.cmds.linear.x  = data.axes[2] * 10.0               # roll
        self.cmds.linear.y  = data.axes[3] * 10.0               # pitch
        self.cmds.angular.z = data.axes[0] * 10.0               # yaw
        self.cmds.linear.z  = 0.25 * (data.axes[1] / 2.0) + 0.5 # thrust

        # Button Controls
        self.arm = data.buttons[9]
        self.magnet_button = data.axes[5]

        if data.buttons[0] == 1.0:
            rp.loginfo("Mission set to 1 - Deploying Sensor")
            self.mission_id = 1
            self.state_id = 0
        if data.buttons[1] == 1.0:
            rp.loginfo("Mission set to 2 - Emergency Escape and Hover")
            self.mission_id = 2
            self.state_id = 0
        if data.buttons[2] == 1.0:
            rp.loginfo("Mission set to 3 - Retreiving Sensor and Exiting")
            self.mission_id = 3
            self.state_id = 0
        if data.buttons[3] == 1.0:
            rp.loginfo("Mission set to 4 - Changing Flight Mode to Landing")
            self.mission_id = 4
            self.state_id = 0
        if data.buttons[4] == 1.0:
            if not self.on_mission:
                rp.loginfo("Starting Mission " + str(self.mission_id))
            else:
                rp.loginfo("Quitting mission")
            self.on_mission = not self.on_mission

    def stateMachine(self, x_drone, y_drone, z_drone):
        if self.mission_id == 1:

            x_des = self.struct_x
            y_des = self.struct_y
            z_des = self.desired_positions_m1[self.state_id] + self.struct_z

            check = self.checkState([x_drone, y_drone, z_drone], [x_des, y_des, z_des])

            if (check[0] < 0.075) & (check[1] < 0.075) & (check[2] < 0.05):
                if (self.state_id == len(self.desired_positions_m1) - 2):
                    if not self.docked:
                        self.state_id += 1
                        rp.loginfo("New target altitude is: " + str(self.desired_positions_m1[self.state_id] + self.struct_z))
                else:
                    self.state_id += 1
                    rp.loginfo("New target altitude is: " + str(self.desired_positions_m1[self.state_id] + self.struct_z))

            z_des = self.desired_positions_m1[self.state_id] + self.struct_z

        elif self.mission_id == 2:

            x_des = self.desired_positions_m2[self.state_id][0]
            y_des = self.desired_positions_m2[self.state_id][1]
            z_des = self.desired_positions_m2[self.state_id][2]

            check = self.checkState([x_drone, y_drone, z_drone], [x_des, y_des, z_des])

            if (check[0] < 0.075) & (check[1] < 0.075) & (check[2] < 0.05):
                if (self.state_id == 1):
                    if self.docked:
                        self.state_id += 1
                        rp.loginfo("New target is: X Pos: " + str(self.desired_positions_m2[self.state_id][0]) + " Y Pos: " + str(self.desired_positions_m2[self.state_id][1]) + " Z Pos: " + str(self.desired_positions_m2[self.state_id][2]))
                else:
                    self.state_id += 1
                    rp.loginfo("New target is: X Pos: " + str(self.desired_positions_m2[self.state_id][0]) + " Y Pos: " + str(self.desired_positions_m2[self.state_id][1]) + " Z Pos: " + str(self.desired_positions_m2[self.state_id][2]))
         
            x_des = self.desired_positions_m2[self.state_id][0]
            y_des = self.desired_positions_m2[self.state_id][1]
            z_des = self.desired_positions_m2[self.state_id][2]

        elif self.mission_id == 3:
            x_des = 0.0
            y_des = -1.8
            z_des = 1.0

        elif self.mission_id == 4:
            rp.loginfo("setting LAND mode")
            self.vehicle.mode = VehicleMode("LAND")
            x_des = x_drone
            y_des = y_drone
            z_des = z_drone

        return [x_des, y_des, z_des]

    def checkState(self, current_p, desired_p):
        dx = abs(current_p[0] - desired_p[0])
        dy = abs(current_p[1] - desired_p[1])
        dz = abs(current_p[2] - desired_p[2])

        return [dx, dy, dz]

    def engage_magnet(self):
        msg_hi = self.vehicle.message_factory.command_long_encode(
                0, 0,   # target_system, target_command
                mavutil.mavlink.MAV_CMD_DO_SET_SERVO, # command
                0,
                8,    # servo number
                2006, # servo position
                0, 0, 0, 0, 0)

        msg_neut = self.vehicle.message_factory.command_long_encode(
                0, 0,   # target_system, target_command
                mavutil.mavlink.MAV_CMD_DO_SET_SERVO, # command
                0,
                8,    # servo number
                1500, # servo position
                0, 0, 0, 0, 0)

        # Send command
        self.vehicle.send_mavlink(msg_hi)
        rp.loginfo("Magnet Engaged")
        time.sleep(5)
        self.vehicle.send_mavlink(msg_neut)
        rp.loginfo("Magnet in Neutral")
        self.docked = True

    def disengage_magnet(self):
        msg_low = self.vehicle.message_factory.command_long_encode(
                0, 0,   # target_system, target_command
                mavutil.mavlink.MAV_CMD_DO_SET_SERVO, # command
                0,
                8,    # servo number
                982,  # servo position
                0, 0, 0, 0, 0)

        msg_neut = self.vehicle.message_factory.command_long_encode(
                0, 0,   # target_system, target_command
                mavutil.mavlink.MAV_CMD_DO_SET_SERVO, # command
                0,
                8,    # servo number
                1500, # servo position
                0, 0, 0, 0, 0)

        # Send command
        self.vehicle.send_mavlink(msg_low)
        rp.loginfo("Magnet Disengaged")
        time.sleep(5)
        self.vehicle.send_mavlink(msg_neut)
        rp.loginfo("Magnet in Neutral")
        self.docked = False

    def send_commands(self):
        rp.loginfo("Accepting Commands")
        r = rp.Rate(self.rate)
        while not rp.is_shutdown():
            # Arm motors
            if self.arm > 0:
                rp.loginfo("Arming...")
                self.arm_and_takeoff_nogps()

            # Mission has started
            if self.on_mission:
                # Generate commands
                uZ = self.kp_z * self.z_error + self.kd_z * self.z_error_d
                uY = self.kp_y * self.y_error + self.kd_y * self.y_error_d
                uX = self.kp_x * self.x_error + self.kd_x * self.x_error_d
                uW = self.kp_w * self.w_error

                linear_z_cmd  = self.clipCommand(uZ + 0.5, 0.65, 0.35)
                linear_y_cmd  = self.clipCommand(uY - 1.3, 7.5, -7.5)
                linear_x_cmd  = self.clipCommand(uX + 0.45, 7.5, -7.5)
                angular_z_cmd = self.clipCommand(uW, 5, -5)

                cmd = TwistStamped()
                cmd.header.stamp = rp.Time.now()
                cmd.twist.angular.x = -linear_y_cmd
                cmd.twist.angular.y = -linear_x_cmd
                cmd.twist.angular.z =  angular_z_cmd
                cmd.twist.linear.z  =  linear_z_cmd
                self.command_pub.publish(cmd)

                if (time.time() - self.lastOnline < 0.5):
                    self.set_attitude(roll_angle = -linear_y_cmd,
                                      pitch_angle = -linear_x_cmd,
                                      yaw_angle = None,
                                      yaw_rate = angular_z_cmd, use_yaw_rate = True, 
                                      thrust = linear_z_cmd,
                                      duration=1.0/self.rate)
                else:
                    self.set_attitude(roll_angle = 0.0,
                                      pitch_angle = 0.0,
                                      yaw_angle = None,
                                      yaw_rate = 0.0, use_yaw_rate = True, 
                                      thrust = 0.5,
                                      duration=1.0/self.rate)
                    rp.logwarn("Connection Lost!!! Hovering")

                if (self.mission_id == 1) & (self.state_id == len(self.desired_positions_m1) - 2) & (self.magnet_engaged):
                    self.magnet_engaged = False
                    t = threading.Thread(target=self.disengage_magnet)
                    t.start()

                if (self.mission_id == 2) & (self.state_id == 1) & (not self.magnet_engaged):
                    self.magnet_engaged = True
                    t = threading.Thread(target=self.engage_magnet)
                    t.start()

            # For manual control of the magnet
            if self.magnet_button == 1:
                self.magnet_engaged = True
                t = threading.Thread(target=self.engage_magnet)
                t.start()
            elif self.magnet_button == -1:
                self.magnet_engaged = False
                t = threading.Thread(target=self.disengage_magnet)
                t.start()

            r.sleep()

# Start Node
magdrone = magdroneControlNode()