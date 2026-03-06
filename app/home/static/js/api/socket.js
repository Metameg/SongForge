
// src/socket.js
import { io } from "https://cdn.socket.io/4.7.1/socket.io.esm.min.js";

export const socket = io(window.location.origin);
