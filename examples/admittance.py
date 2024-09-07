import numpy as np
import optas
from robot import load_robot


class AdmittanceController:
    """
    Initializes and manages an admittance controller for a robotic arm, which
    adjusts the arm's movement based on external forces or velocities while
    maintaining joint limits and velocity constraints. It takes as input the robot's
    current state and desired velocity, returning updated joint positions.

    """
    def __init__(self, lbr_med_num):
        """
        Initializes an optimization problem to control the motion of a robot
        end-effector with desired velocity and minimum joint velocities, while
        respecting joint limits and actuated joint range.

        Args:
            lbr_med_num (int | str): Used to load a specific robot model from an
                external source, such as the KUKA LBR Med arm. It likely identifies
                the robot's numerical ID or configuration.

        """
        # Setup robot
        self.ee_link = "lbr_link_ee"
        self.robot = load_robot(lbr_med_num, [1])
        self.name = self.robot.get_name()

        # Setup builder
        T = 1
        builder = optas.OptimizationBuilder(T, robots=self.robot, derivs_align=True)

        # Set parameters
        qc = builder.add_parameter("qc", self.robot.ndof)  # current joint position
        vg = builder.add_parameter(
            "vg", 6
        )  # task space velocity goal: [vx, vy, vz, wx, wy, wz]
        dt = builder.add_parameter("dt")  # time step

        # Get model states
        dq = builder.get_model_state(self.name, t=0, time_deriv=1)

        # Cost: end-effector goal velocity
        J = self.robot.get_global_link_geometric_jacobian(self.ee_link, qc)
        v = J @ dq
        builder.add_cost_term("ee_vel_goal", 50.0 * optas.sumsqr(v - vg))

        # Constraint: joint limits
        q = qc + dt * dq
        builder.add_bound_inequality_constraint(
            "joint_limits",
            self.robot.lower_actuated_joint_limits,
            q,
            self.robot.upper_actuated_joint_limits,
        )

        # Cost: minimize joint velocity
        builder.add_cost_term("min_joint_vel", optas.sumsqr(dq))

        # Setup solver
        opt = builder.build()
        solver_options = {"printLevel": "none"}
        self.solver = optas.CasADiSolver(opt).setup("qpoases", solver_options)
        self.solution = None
        self.gain = np.array([0.05, 0.05, 0.05, 0.3, 0.3, 0.3])
        self.vlim = np.concatenate(([0.2, 0.2, 0.2], np.deg2rad([40] * 3)))

    def __call__(self, qc, wr, dt):
        """
        Simulates the dynamic behavior of an admittance-controlled system, updating
        the generalized force and position based on initial conditions, solver
        settings, and a time step. It returns the updated position at the specified
        time.

        Args:
            qc (float | int): Referenced as "qc" in several places within the code,
                specifically when resetting parameters for the solver. It represents
                a quantity or value in the simulation.
            wr (float): Used to calculate the value of `vg` by multiplying it with
                the `gain` attribute of the instance, possibly scaling or filtering
                the input value.
            dt (float): Used as time step in solving an initial value problem
                through numerical integration, often representing the size of each
                time step or increment.

        Returns:
            float: The updated quality control value (`qg`) after solving a
            differential equation, representing the state at the next time step
            with respect to current state and input parameters.

        """
        # Admittance control: map force -> velocity
        vg = self.gain * wr

        # Clip velocity (safety)
        vg = np.clip(vg, -self.vlim, self.vlim)

        # Setup solver
        if self.solution is not None:
            self.solver.reset_initial_seed(self.solution)

        self.solver.reset_parameters({"qc": qc, "vg": vg, "dt": dt})

        # Solve problem and retrieve solution
        self.solution = self.solver.solve()
        dqg = self.solution[f"{self.name}/dq"].toarray().flatten()
        qg = qc + dt * dqg

        return qg
